"""
Batch upload IEM Cologne Major 2026 demos to HuggingFace.
Resumable — state stored in .hf_upload_state.json.

Usage:
    python scripts/batch_upload_hf.py [--batch 5] [--start-id 2394774]

Processes N matches per run. Cleans up local files after each upload.
Re-run to continue from last processed match.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import time
from pathlib import Path

from huggingface_hub import HfApi
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

console = Console(force_terminal=True)

REPO = "cs2povarchive/cs2-demos"
HF_ROOT = "iem_cologne_major_2026"
STATE_FILE = Path(".hf_upload_state.json")

# All 86 missing match IDs (Stage 1 + Stage 2 + Stage 3 + Playoffs)
MISSING_MATCHES = [
    # Stage 1 — Round 2+ (IDs 2394840+)
    ("2394774", "heroic-vs-sharks-iem-cologne-major-2026-stage-1"),
    ("2394775", "betboom-vs-gaimin-gladiators-iem-cologne-major-2026-stage-1"),
    ("2394776", "big-vs-liquid-iem-cologne-major-2026-stage-1"),
    ("2394841", "heroic-vs-lynn-vision-iem-cologne-major-2026-stage-1"),
    ("2394843", "sinners-vs-nrg-iem-cologne-major-2026-stage-1"),
    ("2394845", "m80-vs-sharks-iem-cologne-major-2026-stage-1"),
    ("2394847", "big-vs-gaimin-gladiators-iem-cologne-major-2026-stage-1"),
    ("2394848", "nrg-vs-flyquest-iem-cologne-major-2026-stage-1"),
    ("2394849", "sharks-vs-lynn-vision-iem-cologne-major-2026-stage-1"),
    ("2394850", "liquid-vs-mibr-iem-cologne-major-2026-stage-1"),
    ("2394851", "thunder-downunder-vs-big-iem-cologne-major-2026-stage-1"),
    ("2394852", "gamerlegion-vs-betboom-iem-cologne-major-2026-stage-1"),
    ("2394853", "tyloo-vs-sinners-iem-cologne-major-2026-stage-1"),
    ("2394854", "m80-vs-b8-iem-cologne-major-2026-stage-1"),
    ("2394855", "gaimin-gladiators-vs-heroic-iem-cologne-major-2026-stage-1"),
    ("2394856", "thunder-downunder-vs-flyquest-iem-cologne-major-2026-stage-1"),
    ("2394857", "tyloo-vs-sharks-iem-cologne-major-2026-stage-1"),
    ("2394858", "gamerlegion-vs-big-iem-cologne-major-2026-stage-1"),
    ("2394859", "mibr-vs-lynn-vision-iem-cologne-major-2026-stage-1"),
    ("2394860", "liquid-vs-heroic-iem-cologne-major-2026-stage-1"),
    ("2394861", "m80-vs-nrg-iem-cologne-major-2026-stage-1"),
    ("2394862", "tyloo-vs-lynn-vision-iem-cologne-major-2026-stage-1"),
    ("2394863", "liquid-vs-flyquest-iem-cologne-major-2026-stage-1"),
    ("2394864", "nrg-vs-big-iem-cologne-major-2026-stage-1"),
    # Stage 2
    ("2394865", "spirit-vs-betboom-iem-cologne-major-2026-stage-2"),
    ("2394866", "astralis-vs-gamerlegion-iem-cologne-major-2026-stage-2"),
    ("2394867", "g2-vs-m80-iem-cologne-major-2026-stage-2"),
    ("2394868", "fut-vs-b8-iem-cologne-major-2026-stage-2"),
    ("2394869", "legacy-vs-mibr-iem-cologne-major-2026-stage-2"),
    ("2394870", "9z-vs-flyquest-iem-cologne-major-2026-stage-2"),
    ("2394871", "pain-vs-tyloo-iem-cologne-major-2026-stage-2"),
    ("2394872", "monte-vs-big-iem-cologne-major-2026-stage-2"),
    ("2394873", "fut-vs-tyloo-iem-cologne-major-2026-stage-2"),
    ("2394874", "astralis-vs-9z-iem-cologne-major-2026-stage-2"),
    ("2394875", "g2-vs-monte-iem-cologne-major-2026-stage-2"),
    ("2394876", "pain-vs-big-iem-cologne-major-2026-stage-2"),
    ("2394877", "betboom-vs-gamerlegion-iem-cologne-major-2026-stage-2"),
    ("2394878", "legacy-vs-flyquest-iem-cologne-major-2026-stage-2"),
    ("2394880", "b8-vs-m80-iem-cologne-major-2026-stage-2"),
    ("2394881", "monte-vs-legacy-iem-cologne-major-2026-stage-2"),
    ("2394882", "astralis-vs-tyloo-iem-cologne-major-2026-stage-2"),
    ("2394883", "betboom-vs-m80-iem-cologne-major-2026-stage-2"),
    ("2394884", "mibr-vs-big-iem-cologne-major-2026-stage-2"),
    ("2394885", "b8-vs-gamerlegion-iem-cologne-major-2026-stage-2"),
    ("2394886", "flyquest-vs-pain-iem-cologne-major-2026-stage-2"),
    ("2394888", "spirit-vs-9z-iem-cologne-major-2026-stage-2"),
    ("2394889", "astralis-vs-pain-iem-cologne-major-2026-stage-2"),
    ("2394890", "mibr-vs-b8-iem-cologne-major-2026-stage-2"),
    ("2394891", "monte-vs-betboom-iem-cologne-major-2026-stage-2"),
    ("2394892", "tyloo-vs-9z-iem-cologne-major-2026-stage-2"),
    ("2394893", "g2-vs-big-iem-cologne-major-2026-stage-2"),
    ("2394894", "m80-vs-legacy-iem-cologne-major-2026-stage-2"),
    ("2394895", "monte-vs-pain-iem-cologne-major-2026-stage-2"),
    ("2394896", "legacy-vs-tyloo-iem-cologne-major-2026-stage-2"),
    # Stage 3
    ("2394901", "furia-vs-b8-iem-cologne-major-2026"),
    ("2394902", "aurora-vs-monte-iem-cologne-major-2026"),
    ("2394903", "parivision-vs-9z-iem-cologne-major-2026"),
    ("2394905", "mouz-vs-legacy-iem-cologne-major-2026"),
    ("2394971", "furia-vs-mouz-iem-cologne-major-2026"),
    ("2394972", "the-mongolz-vs-b8-iem-cologne-major-2026"),
    ("2394973", "fut-vs-g2-iem-cologne-major-2026"),
    ("2394974", "parivision-vs-monte-iem-cologne-major-2026"),
    ("2394975", "falcons-vs-betboom-iem-cologne-major-2026"),
    ("2394976", "natus-vincere-vs-legacy-iem-cologne-major-2026"),
    ("2394977", "aurora-vs-spirit-iem-cologne-major-2026"),
    ("2394979", "natus-vincere-vs-the-mongolz-iem-cologne-major-2026"),
    ("2394980", "b8-vs-fut-iem-cologne-major-2026"),
    ("2394981", "aurora-vs-g2-iem-cologne-major-2026"),
    ("2394982", "falcons-vs-monte-iem-cologne-major-2026"),
    ("2394983", "vitality-vs-mouz-iem-cologne-major-2026"),
    ("2394984", "parivision-vs-legacy-iem-cologne-major-2026"),
    ("2394985", "betboom-vs-furia-iem-cologne-major-2026"),
    ("2394986", "spirit-vs-9z-iem-cologne-major-2026"),
    ("2394987", "mouz-vs-fut-iem-cologne-major-2026"),
    ("2394988", "the-mongolz-vs-monte-iem-cologne-major-2026"),
    ("2394989", "betboom-vs-vitality-iem-cologne-major-2026"),
    ("2394990", "aurora-vs-9z-iem-cologne-major-2026"),
    ("2394991", "natus-vincere-vs-falcons-iem-cologne-major-2026"),
    ("2394992", "g2-vs-legacy-iem-cologne-major-2026"),
    ("2394993", "9z-vs-the-mongolz-iem-cologne-major-2026"),
    ("2394994", "betboom-vs-fut-iem-cologne-major-2026"),
    ("2394995", "natus-vincere-vs-g2-iem-cologne-major-2026"),
    # Playoffs
    ("2394999", "falcons-vs-vitality-iem-cologne-major-2026"),
    ("2395000", "aurora-vs-furia-iem-cologne-major-2026"),
    ("2395001", "spirit-vs-falcons-iem-cologne-major-2026"),
    ("2395002", "furia-vs-falcons-iem-cologne-major-2026"),
]


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"completed": [], "failed": [], "in_progress": None}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def hf_folder_name(slug: str) -> str:
    """Normalize HLTV slug to HF folder name (strip trailing year)."""
    name = slug.lower()
    name = re.sub(r"-\d{4}$", "", name)
    return name


def download_match(match_id: str, slug: str) -> Path | None:
    """Download and extract demo via acquire_match."""
    from scrapers.hltv_acquire import acquire_match, match_slug_from_url, match_demo_dir

    url = f"https://www.hltv.org/matches/{match_id}/{slug}"
    console.print(f"\n  [cyan]Downloading:[/cyan] {url}")
    try:
        result = acquire_match(url, force=True, headless=False)
        folder = match_demo_dir(match_slug_from_url(url))
        if result.error:
            console.print(f"  [red]Download failed: {result.error}[/red]")
            return None
        return folder if folder.exists() else None
    except Exception as e:
        console.print(f"  [red]Download exception: {e}[/red]")
        return None


def upload_folder(folder: Path, hf_path: str) -> bool:
    """Upload all .dem files from folder to HF."""
    api = HfApi()
    dem_files = list(folder.glob("*.dem"))
    if not dem_files:
        console.print(f"  [yellow]No .dem files in {folder}[/yellow]")
        return False

    for dem_path in dem_files:
        hf_dest = f"{hf_path}/{dem_path.name}"
        console.print(f"  [cyan]Uploading:[/cyan] {dem_path.name} -> {hf_dest}")
        try:
            api.upload_file(
                path_or_fileobj=str(dem_path),
                path_in_repo=hf_dest,
                repo_id=REPO,
                repo_type="dataset",
            )
        except Exception as e:
            console.print(f"  [red]Upload failed for {dem_path.name}: {e}[/red]")
            return False
    return True


def cleanup_local(folder: Path) -> None:
    """Remove local demo folder and archives."""
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)
    # Also clean up archive parent
    archive_dir = folder.parent
    if archive_dir.exists():
        for f in archive_dir.iterdir():
            if f.suffix.lower() in (".rar", ".zip", ".7z"):
                f.unlink(missing_ok=True)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Batch upload demos to HF")
    parser.add_argument("--batch", type=int, default=3, help="Matches per run (default: 3)")
    parser.add_argument("--start-id", type=str, help="Start from this match ID")
    parser.add_argument("--download-only", action="store_true", help="Download only, skip upload + cleanup")
    parser.add_argument("--upload-only", action="store_true", help="Upload only, skip download")
    args = parser.parse_args()

    state = load_state()
    completed_set = set(state["completed"])
    failed_set = set(state["failed"])

    # Filter matches — include failed (retry them)
    to_process = [(mid, slug) for mid, slug in MISSING_MATCHES
                  if mid not in completed_set]

    if args.start_id:
        try:
            idx = next(i for i, (mid, _) in enumerate(to_process) if mid == args.start_id)
            to_process = to_process[idx:]
        except StopIteration:
            console.print(f"[red]start-id {args.start_id} not found[/red]")
            sys.exit(1)

    if not to_process:
        console.print("[green]All matches already processed![/green]")
        return

    batch = to_process[:args.batch]
    console.print(f"[bold]Processing {len(batch)} matches[/bold] (batch {args.batch})")
    console.print(f"  Completed: {len(completed_set)}, Failed: {len(failed_set)}")
    console.print(f"  Remaining: {len(to_process)}")

    for mid, slug in batch:
        console.print(f"\n[bold cyan]=== [{mid}] {slug} ===[/bold cyan]")

        state["in_progress"] = mid
        save_state(state)

        hf_name = hf_folder_name(slug)
        hf_path = f"{HF_ROOT}/{hf_name}"

        if args.upload_only:
            from scrapers.hltv_acquire import match_slug_from_url, match_demo_dir
            url = f"https://www.hltv.org/matches/{mid}/{slug}"
            slug_local = match_slug_from_url(url)
            folder = match_demo_dir(slug_local)
            if not folder.exists() or not list(folder.glob("*.dem")):
                console.print(f"  [yellow]No .dem files for {slug}, skipping[/yellow]")
                state["failed"].append(mid)
                state["in_progress"] = None
                save_state(state)
                continue
            console.print(f"  [cyan]Uploading existing:[/cyan] {folder}")
            if not upload_folder(folder, hf_path):
                state["failed"].append(mid)
                state["in_progress"] = None
                save_state(state)
                continue
            state["completed"].append(mid)
            state["failed"].discard(mid)
            state["in_progress"] = None
            save_state(state)
            cleanup_local(folder)
            console.print(f"  [green]Done: {slug}[/green]")
            continue

        # 1) Download + extract
        folder = download_match(mid, slug)
        if folder is None:
            state["failed"].append(mid)
            state["in_progress"] = None
            save_state(state)
            continue

        if args.download_only:
            state["completed"].append(mid)
            state["failed"].discard(mid)
            state["in_progress"] = None
            save_state(state)
            console.print(f"  [green]Downloaded: {slug}[/green]")
            continue

        # 2) Upload
        if not upload_folder(folder, hf_path):
            state["failed"].append(mid)
            state["in_progress"] = None
            save_state(state)
            cleanup_local(folder)
            continue

        # 3) Record success
        state["completed"].append(mid)
        state["failed"].discard(mid)
        state["in_progress"] = None
        save_state(state)

        # 4) Clean up
        cleanup_local(folder)
        console.print(f"  [green]Done: {slug}[/green]")

    console.print(f"\n[bold]Batch complete.[/bold]")
    console.print(f"  Completed: {len(state['completed'])}")
    console.print(f"  Failed: {len(state['failed'])}")
    console.print(f"  Re-run to continue next batch.")


if __name__ == "__main__":
    main()
