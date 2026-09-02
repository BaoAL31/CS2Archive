"""Daily YouTube star refresh: player demand + highlight team demand.

Scrapes competitor POV channels plus @cs2povarchive, and official highlight
channels, then rewrites:

  .data/player_demand_index.json
  .data/team_demand_index.json

Usage:
    python scripts/hltv/refresh_stars.py
    python scripts/hltv/refresh_stars.py --offline
    python scripts/hltv/refresh_stars.py --install-cron --at 12:00
    python scripts/hltv/refresh_stars.py --remove-cron
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from _pathsetup import ensure  # noqa: E402

ensure()

from scrape_pov_channels import DEFAULT_CHANNELS  # noqa: E402
from update_player_demand import refresh as refresh_player_demand  # noqa: E402
from update_team_demand import refresh as refresh_team_demand  # noqa: E402

TASK_NAME = "CS2ArchiveStarRefresh"
LAUNCHER = ROOT / "scripts" / "hltv" / "run_refresh_stars.ps1"
DEFAULT_AT = "12:00"


def _task_command() -> str:
    return (
        "powershell.exe -NoProfile -ExecutionPolicy Bypass "
        f'-File "{LAUNCHER}"'
    )


def install_cron(at: str = DEFAULT_AT) -> None:
    if len(at) != 5 or at[2] != ":":
        raise ValueError(f"--at must be HH:MM, got {at!r}")
    subprocess.run(
        ["schtasks.exe", "/Delete", "/TN", TASK_NAME, "/F"],
        capture_output=True, check=False,
    )
    created = subprocess.run(
        [
            "schtasks.exe", "/Create", "/TN", TASK_NAME,
            "/SC", "DAILY", "/ST", at, "/RL", "HIGHEST",
            "/TR", _task_command(), "/F",
        ],
        capture_output=True, text=True,
    )
    if created.returncode != 0:
        created = subprocess.run(
            [
                "schtasks.exe", "/Create", "/TN", TASK_NAME,
                "/SC", "DAILY", "/ST", at,
                "/TR", _task_command(), "/F",
            ],
            capture_output=True, text=True,
        )
    if created.returncode != 0:
        sys.stderr.write(created.stderr or created.stdout or "schtasks failed\n")
        raise SystemExit(created.returncode)
    print(f"Installed {TASK_NAME} daily at {at} (local time)")
    print(f"  {_task_command()}")
    print(f"Run now: schtasks.exe /Run /TN \"{TASK_NAME}\"")


def remove_cron() -> None:
    r = subprocess.run(
        ["schtasks.exe", "/Delete", "/TN", TASK_NAME, "/F"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(r.stderr or r.stdout or f"{TASK_NAME} not found")
        return
    print(f"Removed {TASK_NAME}")


def refresh(*, scrape: bool = True) -> int:
    failed = 0
    print(f"[STARS] player scrape channels: {len(DEFAULT_CHANNELS)} "
          f"(includes @cs2povarchive)")
    try:
        demand = refresh_player_demand(scrape=scrape)
        print(
            f"[PLAYER] {len(demand['index'])} players from "
            f"{demand['window_videos']} videos "
            f"(scraped {demand.get('scraped', 0)} new/updated)"
        )
    except Exception as exc:
        failed += 1
        print(f"[PLAYER] failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    try:
        team = refresh_team_demand(scrape=scrape)
        print(
            f"[TEAM] {len(team['index'])} teams, "
            f"{team['durable_videos']} durable / {team['history_videos']} videos "
            f"(scraped {team.get('scraped', 0)})"
        )
    except Exception as exc:
        failed += 1
        print(f"[TEAM] failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    try:
        from shorts.fit_partial_stars import (
            DAILY_NEW_MATCHES,
            harvest_allstar,
            listener_holds_cloak,
            refresh_partial_stars,
        )
        if scrape and not listener_holds_cloak():
            print(f"[SHORTS] Allstar harvest (max {DAILY_NEW_MATCHES} unseen match pages)")
            harvest_allstar(max_matches=DAILY_NEW_MATCHES)
        elif scrape:
            print("[SHORTS] skip Allstar harvest: listener holds CloakBrowser")
        stars = refresh_partial_stars(fetch_views=scrape)
        print(
            f"[SHORTS] Partial stars intercept={stars['intercept']:.3f} "
            f"rows={stars.get('_rows', 0)}"
        )
    except Exception as exc:
        failed += 1
        print(f"[SHORTS] failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    return failed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--offline", action="store_true",
        help="Recompute from stored history only (no YouTube scrape)",
    )
    ap.add_argument(
        "--install-cron", action="store_true",
        help=f"Install Windows task {TASK_NAME} (daily, local time)",
    )
    ap.add_argument(
        "--remove-cron", action="store_true",
        help=f"Delete Windows task {TASK_NAME}",
    )
    ap.add_argument(
        "--at", default=DEFAULT_AT,
        help=f"Daily clock time for --install-cron (default {DEFAULT_AT})",
    )
    args = ap.parse_args()
    if args.install_cron:
        install_cron(args.at)
        return 0
    if args.remove_cron:
        remove_cron()
        return 0
    return refresh(scrape=not args.offline)


if __name__ == "__main__":
    raise SystemExit(main())
