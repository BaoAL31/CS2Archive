"""Poll HLTV results and render the highest-weighted POV per match.

Cards are scored with highlight-channel team demand, POV-channel player
demand, org rank, and HLTV rating. One card per match is queued from
``backlog/<match>/{high,medium}/``.

Cap is 3 uploads per local calendar day (the YouTube long-form slots).
When the configured event has nothing live and nothing starting in the
next 12 hours, the listener keeps polling FACEIT for watchable POVs
(plus-K/D win from a player on the YouTube demand index). It queues those
as they appear, up
to the remaining daily slots, and does not pad with weak games.

The listener is intentionally a single process and a single pipeline worker.
It persists state in ``.listener/hltv.json`` so a restart does not repeat
downloads or renders. When a POV is youtube-ready, upload starts in a new
console via ``scripts/upload/upload_pending.py --dir <overlay> --limit 1``
so leftover pending metas under ``youtube/`` are not picked up.
The listener then pops the queue and renders the next card.

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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = ROOT / "scripts"
for _p in (str(ROOT), str(_SCRIPTS), str(_SCRIPTS / "faceit")):
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
from scrapers.hltv_acquire import (  # noqa: E402
    fetch_hltv_page_html,
    match_id_from_url,
    match_slug_from_url,
)
from daily_notable import (  # noqa: E402
    DEFAULT_HOURS,
    discover_good_povs,
    download_and_backlog,
    rel_card_for_pick,
    remember_picks,
)

DEFAULT_EVENT_URL = "https://www.hltv.org/events/8249/blast-open-porto-2026"
DEFAULT_STATE = ROOT / ".listener" / "hltv.json"
DEFAULT_RANKINGS_URL = "https://www.hltv.org/ranking/teams"
MIN_RATING = 1.5
DAILY_UPLOAD_LIMIT = 3
FACEIT_HORIZON = timedelta(hours=12)
FACEIT_SCRAPE_INTERVAL = timedelta(minutes=15)


@dataclass
class Match:
    match_id: str
    url: str
    slug: str
    team1: str
    team2: str
    event: str = ""


@dataclass
class ScheduledMatch:
    match_id: str
    unix_ms: int | None = None
    live: bool = False
    slug: str = ""


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


_ORDINAL = re.compile(r"(\d+)(?:st|nd|rd|th)", re.I)


def parse_results_headline_date(text: str) -> date | None:
    """Parse HLTV result group titles like 'Results for August 31st 2026'."""
    raw = re.sub(r"^results for\s+", "", (text or "").strip(), flags=re.I)
    raw = _ORDINAL.sub(r"\1", raw)
    try:
        return datetime.strptime(raw, "%B %d %Y").date()
    except ValueError:
        return None


def _match_from_result_con(node, base_url: str) -> Match | None:
    link = node.select_one('a[href*="/matches/"]')
    if link is None:
        return None
    parsed = _canonical_match(link.get("href", ""))
    if not parsed:
        return None
    match_id, slug = parsed
    event_node = node.select_one(".event-name")
    team1, team2 = _slug_teams(slug)
    return Match(
        match_id=match_id,
        url=urljoin(base_url, link["href"]),
        slug=slug,
        team1=team1,
        team2=team2,
        event=event_node.get_text(" ", strip=True) if event_node else "",
    )


def parse_match_links(
    html: str,
    base_url: str = settings.hltv_base_url,
    *,
    on_dates: set[date] | None = None,
) -> list[Match]:
    """Parse unique completed-match links from a results page.

    HLTV ignores ``?date=`` on ``/results`` (the SPA always returns the
    latest-results dump). Date filtering uses on-page ``.standard-headline``
    groups such as ``Results for August 31st 2026``.
    """
    soup = BeautifulSoup(html, "lxml")
    holder = soup.select_one(".results-holder")
    found: list[Match] = []
    seen: set[str] = set()
    if holder is not None:
        headline_date: date | None = None
        for node in holder.descendants:
            if not getattr(node, "name", None):
                continue
            classes = node.get("class") or []
            if "standard-headline" in classes:
                headline_date = parse_results_headline_date(
                    node.get_text(" ", strip=True))
                continue
            if node.name != "div" or "result-con" not in classes:
                continue
            # Featured results (and any block with no parseable date) are the
            # live dump's latest completed matches. Only dated headlines
            # outside on_dates are skipped.
            if (
                on_dates is not None
                and headline_date is not None
                and headline_date not in on_dates
            ):
                continue
            match = _match_from_result_con(node, base_url)
            if match is None or match.match_id in seen:
                continue
            seen.add(match.match_id)
            found.append(match)
        return found
    for link in soup.select('a[href*="/matches/"]'):
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


def event_matches_url(event_url: str, base_url: str = settings.hltv_base_url) -> str:
    """HLTV event matches tab (upcoming + live + results for this event)."""
    match = re.search(r"/events/(\d+)", event_url)
    if not match:
        return event_url
    return f"{base_url.rstrip('/')}/events/{match.group(1)}/matches"


def _as_unix_ms(raw: int) -> int:
    return raw * 1000 if raw < 10_000_000_000 else raw


def _unix_from_node(node) -> int | None:
    for attr in ("data-zonedgrouping-entry-unix", "data-unix"):
        raw = node.get(attr) if hasattr(node, "get") else None
        if not raw:
            continue
        try:
            return _as_unix_ms(int(raw))
        except (TypeError, ValueError):
            continue
    child = node.select_one("[data-unix], [data-zonedgrouping-entry-unix]")
    if child is not None and child is not node:
        return _unix_from_node(child)
    return None


def _is_live(node) -> bool:
    """True when HLTV marks the fixture live.

    The matches tab reuses class ``matchLive`` on star-rating chips, so that
    class is not a live signal. Prefer the ``live`` attribute on
    ``.match-wrapper``; fall back to the older live-container / live-time
    markup.
    """
    wrapper = node if node.get("live") is not None else node.select_one("[live]")
    if wrapper is not None and wrapper.get("live") is not None:
        return str(wrapper.get("live")).strip().lower() in {"true", "1", "yes"}
    classes = " ".join(node.get("class") or [])
    if "liveMatch" in classes or "live-match" in classes:
        return True
    return bool(node.select_one(".matchTime.matchLive"))


def parse_scheduled_matches(html: str) -> list[ScheduledMatch]:
    """Parse live/upcoming matches (with start timestamps) from an event page."""
    soup = BeautifulSoup(html, "lxml")
    nodes = soup.select(
        ".upcomingMatch, .liveMatch-container, a.upcoming-match, .live-match"
    )
    if not nodes:
        nodes = soup.select("[data-zonedgrouping-entry-unix]")
    found: list[ScheduledMatch] = []
    seen: set[str] = set()
    for node in nodes:
        unix_ms = _unix_from_node(node)
        parsed = _canonical_match(node.get("href") or "")
        if not parsed:
            for link in node.select('a[href*="/matches/"]'):
                parsed = _canonical_match(link.get("href", ""))
                if parsed:
                    break
        if not parsed:
            continue
        match_id, slug = parsed
        if match_id in seen:
            continue
        seen.add(match_id)
        found.append(ScheduledMatch(
            match_id=match_id, unix_ms=unix_ms, live=_is_live(node), slug=slug))
    return found


def _scheduled_when(item: ScheduledMatch) -> datetime | None:
    if item.unix_ms is None:
        return None
    try:
        return datetime.fromtimestamp(item.unix_ms / 1000)
    except (OSError, OverflowError, ValueError):
        return None


def upcoming_within(
    scheduled: list[ScheduledMatch],
    now: datetime | None = None,
    horizon: timedelta = FACEIT_HORIZON,
) -> list[ScheduledMatch]:
    """Live matches, plus timed fixtures at or before ``now + horizon``."""
    now = now or datetime.now()
    until = now + horizon
    found: list[ScheduledMatch] = []
    for item in scheduled:
        if item.live:
            found.append(item)
            continue
        when = _scheduled_when(item)
        if when is not None and now <= when <= until:
            found.append(item)
    return found


def event_busy(
    scheduled: list[ScheduledMatch],
    now: datetime | None = None,
) -> bool:
    """True when the event is live or a match starts within 12h."""
    return bool(upcoming_within(scheduled, now=now))


def format_scheduled(
    scheduled: list[ScheduledMatch],
    now: datetime | None = None,
    limit: int = 6,
) -> str:
    """Human-readable upcoming/live fixtures for the poll log."""
    now = now or datetime.now()
    rows: list[tuple[datetime, str]] = []
    undated: list[str] = []
    for item in scheduled:
        label = item.slug or item.match_id
        if item.live:
            rows.append((now, f"{label} LIVE"))
            continue
        when = _scheduled_when(item)
        if when is None:
            undated.append(label)
            continue
        hours = (when - now).total_seconds() / 3600
        rows.append((when, f"{label} {when:%Y-%m-%d %H:%M} ({hours:+.1f}h)"))
    rows.sort(key=lambda row: row[0])
    parts = [text for _when, text in rows[:limit]]
    if undated:
        parts.append(f"{len(undated)} undated")
    return "; ".join(parts) if parts else "none"


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
        "daily": {"day": None, "completed": []},
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


def _mark_existing_backlog_done(record: dict, cards: list[str]) -> None:
    """Existing cards mean this match was already processed. Do not re-render."""
    record["status"] = "completed"
    record["cards"] = cards
    done = list(record.get("completed_cards") or [])
    for card in cards:
        if card not in done:
            done.append(card)
    record["completed_cards"] = done
    record["skip_reason"] = "existing backlog"
    record["last_error"] = None


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
    """Higher star_score (log card + fitted kind lift), then rating."""
    try:
        score = float(meta.get("star_score", meta.get("weight") or 0))
    except (TypeError, ValueError):
        score = 0.0
    try:
        rating = float(meta.get("rating") or 0)
    except (TypeError, ValueError):
        rating = 0.0
    return (score, rating)


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
        group = (
            str(meta.get("hltv_url") or "").strip()
            or str(meta.get("faceit_match_id") or "").strip()
            or str(full.parent.parent)
        )
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


def _local_day() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _daily(state: State) -> dict:
    day = _local_day()
    daily = state.data.setdefault("daily", {"day": None, "completed": []})
    if daily.get("day") != day:
        daily["day"] = day
        daily["completed"] = []
        daily["faceit_queued"] = []
        daily["faceit_attempted"] = False
    return daily


def _slots_left(state: State) -> int:
    return max(0, DAILY_UPLOAD_LIMIT - len(_daily(state).get("completed") or []))


def _queue_room(state: State) -> int:
    return max(0, _slots_left(state) - len(state.data.get("queue") or []))


def _is_faceit_card(path: str, meta: dict | None = None) -> bool:
    if meta and (meta.get("is_faceit") or meta.get("faceit_match_id")):
        return True
    norm = path.replace("\\", "/")
    return norm.startswith("backlog/faceit/") or norm.startswith("faceit/")


def has_pending_hltv(state: State) -> bool:
    for path in state.data.get("queue") or []:
        if not _is_faceit_card(path):
            return True
    for rec in (state.data.get("matches") or {}).values():
        if rec.get("status") in {"discovered", "queued", "retry", "running"}:
            return True
    return False


def should_poll_faceit(state: State, hltv_busy: bool,
                       now: datetime | None = None) -> bool:
    if hltv_busy or has_pending_hltv(state):
        return False
    if _slots_left(state) <= 0:
        return False
    now = now or datetime.now()
    last = _daily(state).get("faceit_last_scrape")
    if last:
        try:
            when = datetime.fromisoformat(last)
        except ValueError:
            when = None
        else:
            if now - when < FACEIT_SCRAPE_INTERVAL:
                return False
    return True


async def _maybe_queue_faceit(args, state: State, indexes: dict | None) -> None:
    room = _queue_room(state)
    if room <= 0:
        return
    daily = _daily(state)
    daily["faceit_last_scrape"] = datetime.now().isoformat()
    if args.dry_run:
        print("[faceit-notable] would scrape FACEIT for watchable POVs",
              flush=True)
        state.save()
        return
    print(
        f"[faceit-notable] no HLTV match in the next 12h; "
        f"scraping last {DEFAULT_HOURS}h for watchable POVs "
        f"({room} slot(s))",
        flush=True,
    )
    picks = await discover_good_povs(
        n=room,
        hours=DEFAULT_HOURS,
        skip_youtube_demand=True,
    )
    if not picks:
        print("[faceit-notable] no watchable FACEIT POVs this scrape",
              flush=True)
        state.save()
        return
    for pick in picks:
        print(
            f"[faceit-notable] pick {pick.get('player')} "
            f"{pick.get('kills')}/{pick.get('deaths')} "
            f"K/D {pick.get('kd')} ADR {pick.get('adr')} "
            f"{pick.get('map')} [{pick.get('match_id')}]",
            flush=True,
        )
    missing = [pick for pick in picks if rel_card_for_pick(pick) is None]
    if missing:
        await asyncio.to_thread(download_and_backlog, missing)
    cards: list[str] = []
    queued_picks: list[dict] = []
    for pick in picks:
        card = rel_card_for_pick(pick)
        if not card:
            print(
                f"[faceit-notable] no backlog card for {pick.get('player')} "
                f"{pick.get('match_id')}",
                flush=True,
            )
            continue
        cards.append(card)
        queued_picks.append(pick)
        if len(cards) >= room:
            break
    remember_picks(queued_picks)
    queued = list(daily.get("faceit_queued") or [])
    for card in cards:
        if card not in queued:
            queued.append(card)
    daily["faceit_queued"] = queued
    if cards:
        _enqueue(state, cards, indexes)
        print(f"[faceit-notable] queued {len(cards)} watchable card(s)",
              flush=True)
    else:
        print("[faceit-notable] no FACEIT cards to queue", flush=True)
    state.save()


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


def _run_id_from_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:80].strip("_")


def _youtube_run_id_for_meta(meta: dict) -> str | None:
    """Same ``run_id`` pipeline.py uses for ``youtube/{run_id}_overlay/``."""
    player = (meta.get("player") or "").strip()
    map_name = (meta.get("map") or "").strip()
    if not player or not map_name:
        return None
    demo_path = Path(meta.get("demo_path") or "")
    is_faceit = bool(meta.get("is_faceit")) or bool(meta.get("faceit_match_id"))
    if is_faceit:
        match_id = meta.get("faceit_match_id") or (
            demo_path.stem.split(" - ")[0] if demo_path.name else "faceit"
        )
        slug = match_id
    else:
        hltv_url = meta.get("hltv_url") or ""
        match_id = match_id_from_url(hltv_url)
        if not match_id:
            return None
        slug = hltv_url.rstrip("/").split("/")[-1]
    dem_stem = demo_path.stem if demo_path.name else slug
    return _run_id_from_name(f"{match_id}_{dem_stem}_{player}_{map_name}")


def _is_youtube_pending(meta: dict) -> bool:
    return not (meta.get("upload_status") == "completed" and meta.get("youtube_id"))


def _pending_upload_metas(card: str, *, root: Path = ROOT) -> list[Path]:
    """Overlay meta for this card, else raw. Never scans ``youtube/``."""
    try:
        meta = json.loads((root / card).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    run_id = _youtube_run_id_for_meta(meta)
    if not run_id:
        return []
    overlay = root / "youtube" / f"{run_id}_overlay" / "upload_meta.json"
    raw = root / "youtube" / run_id / "upload_meta.json"
    chosen = overlay if overlay.exists() else raw
    if not chosen.exists():
        return []
    try:
        data = json.loads(chosen.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    video = data.get("video_path")
    if not _is_youtube_pending(data):
        return []
    if not video or not Path(video).exists():
        return []
    return [chosen]


def _upload_cmd(meta_path: Path) -> list[str] | None:
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    video = data.get("video_path")
    if not video or not Path(video).exists():
        return None
    cmd = [
        sys.executable, "-u",
        str(ROOT / "scripts/upload/upload_pending.py"),
        "--dir", str(meta_path.parent),
        "--limit", "1",
    ]
    return cmd


def _spawn_upload_terminal(cmd: list[str], dry_run: bool) -> None:
    """Fire-and-forget: new console, listener does not wait."""
    print(f"[upload] {' '.join(cmd)}", flush=True)
    if dry_run:
        return
    env = _child_env()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    kwargs: dict = {"cwd": str(ROOT), "env": env}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
    try:
        subprocess.Popen(cmd, **kwargs)
    except OSError as exc:
        print(f"[upload] spawn failed: {exc}", flush=True)


def _start_upload_after_pipeline(card: str, dry_run: bool) -> None:
    if dry_run:
        print(f"[upload] would spawn upload_pending.py for {card}", flush=True)
        return
    metas = _pending_upload_metas(card)
    if not metas:
        print(f"[upload] no pending meta for {card}", flush=True)
        return
    for meta_path in metas:
        cmd = _upload_cmd(meta_path)
        if not cmd:
            print(f"[upload] skip (no video): {meta_path}", flush=True)
            continue
        _spawn_upload_terminal(cmd, dry_run=False)


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
    _daily(state)
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
    scheduled = parse_scheduled_matches(event_html)
    if not any(item.live or item.unix_ms for item in scheduled):
        tab = event_matches_url(event_url)
        if tab != event_url:
            tab_html = await asyncio.to_thread(
                fetch_hltv_page_html, tab, headless=True, wait_selector=None)
            extra = parse_scheduled_matches(tab_html)
            if extra:
                scheduled = extra
            event_ids |= parse_event_match_ids(tab_html)
    now = datetime.now()
    today = now.date()
    event_slug = event_url.rstrip("/").rsplit("/", 1)[-1]
    event_name = re.sub(r"[-_]+", " ", event_slug).casefold()
    # ``?date=`` is ignored by HLTV; one /results dump, filter by headline date.
    results_html = await asyncio.to_thread(
        fetch_hltv_page_html,
        f"{settings.hltv_base_url}/results",
        headless=True,
        wait_selector=".results-holder .result-con",
    )
    result_links = parse_match_links(
        results_html, on_dates={today, today - timedelta(days=1)})
    matches = select_matches(result_links, event_ids, state.data["teams"],
                             event_name)
    matches = list({match.match_id: match for match in matches}.values())
    hltv_busy = event_busy(scheduled, now)
    print(
        f"[poll] {len(matches)} notable completed event match(es); "
        f"hltv_next_12h={'yes' if hltv_busy else 'no'}; "
        f"{_slots_left(state)}/{DAILY_UPLOAD_LIMIT} upload slot(s) left",
        flush=True,
    )
    print(f"[schedule] {format_scheduled(scheduled, now)}", flush=True)
    if not state.data.get("result_baseline_initialized"):
        initialize_result_baseline(state, matches)
        state.save()
        print(f"[baseline] skipped {len(matches)} existing completed match(es)",
              flush=True)
    else:
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
            if _queue_room(state) <= 0:
                print(f"[daily] {DAILY_UPLOAD_LIMIT}/day cap, "
                      f"deferring {match.match_id}", flush=True)
                continue
            cards = _candidate_cards(match, indexes)
            if cards:
                # Already backlogged (usually already rendered). A rebaseline
                # plus a later results-page appearance must not queue it again.
                print(
                    f"[backlog] already have cards for {match.match_id}, "
                    f"not re-rendering",
                    flush=True,
                )
                _mark_existing_backlog_done(record, cards)
                state.save()
                continue
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

    if should_poll_faceit(state, hltv_busy):
        await _maybe_queue_faceit(args, state, indexes)

    while state.data["queue"] and _slots_left(state) > 0:
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
        _start_upload_after_pipeline(card, args.dry_run)
        state.data["queue"].pop(0)
        _daily(state).setdefault("completed", []).append(card)
        for record in state.data["matches"].values():
            if card in record.get("cards", []):
                record.setdefault("completed_cards", []).append(card)
                record["status"] = "completed"
        state.save()
    if state.data["queue"] and _slots_left(state) <= 0:
        print(f"[daily] {DAILY_UPLOAD_LIMIT} uploads used for {_local_day()}, "
              f"holding {len(state.data['queue'])} queued card(s)", flush=True)


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
