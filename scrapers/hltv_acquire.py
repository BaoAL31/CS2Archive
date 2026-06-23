"""
HLTV demo acquisition via CloakBrowser (download + extract + map selection).
Used by pipeline step 1 and `main.py hltv match`.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from rich.console import Console

from config import settings
from downloader import extract_demo, file_size_mb, is_already_downloaded, record_download
from models import DemoSource, DownloadResult, DownloadStatus, MatchInfo

console = Console(force_terminal=True)

ARCHIVE_EXTENSIONS = {".rar", ".zip", ".7z"}
MIN_ARCHIVE_BYTES = 1_000_000
CDP_PROFILE_DIR = Path(".cdp-hltv-profile")
DEFAULT_PROFILE_DIR = CDP_PROFILE_DIR


_CDP_BROWSER_CACHE: dict = {}
_CDP_PORT = 9222


def _kill_stale_cdp_chrome() -> None:
    """Kill Chrome process on port 9222 if it's ours (temp profile). Doesn't touch user Chrome."""
    import subprocess, time
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             'netstat -ano | Select-String ":9222" | Select-String "LISTENING"'],
            capture_output=True, text=True, timeout=10,
        )
        for line in r.stdout.splitlines():
            parts = line.strip().split()
            pid = parts[-1] if parts else ""
            if not pid.isdigit():
                continue
            cmd = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f'Get-CimInstance Win32_Process -Filter "ProcessId={pid}" | Select-Object -ExpandProperty CommandLine'],
                capture_output=True, text=True, timeout=5,
            )
            if "hltv-cdp-" in cmd.stdout or "temp" in cmd.stdout.lower():
                subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, timeout=5)
                time.sleep(1)
    except Exception:
        pass


def _ensure_cdp_browser():
    """Return browser handle (reused if already connected). Launches Chrome if needed."""
    from playwright.sync_api import sync_playwright
    import subprocess, socket, time, tempfile

    cached = _CDP_BROWSER_CACHE.get("browser")
    if cached is not None:
        try:
            cached.contexts  # verify alive
            return cached
        except Exception:
            _CDP_BROWSER_CACHE.pop("browser", None)
            _CDP_BROWSER_CACHE.pop("pw", None)

    _kill_stale_cdp_chrome()

    # Launch Chrome with CDP port + temp profile (no conflict with any running Chrome)
    tmp_profile = Path(tempfile.mkdtemp(prefix="hltv-cdp-"))
    subprocess.Popen(
        [
            "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
            f"--remote-debugging-port={_CDP_PORT}",
            f"--user-data-dir={tmp_profile.resolve()}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            "--no-sandbox",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(3)

    pw = sync_playwright().start()
    for _ in range(10):
        try:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{_CDP_PORT}")
            browser.contexts  # verify live
            _CDP_BROWSER_CACHE["browser"] = browser
            _CDP_BROWSER_CACHE["pw"] = pw
            return browser
        except Exception:
            time.sleep(2)
    raise RuntimeError("Could not start or connect to Chrome CDP")


def _navigate_with_retry(
    page,
    url: str,
    *,
    wait_selector: str | None,
    timeout_ms: int,
    max_retries: int = 10,
) -> str:
    """Navigate to URL, retry on network errors (ERR_CONNECTION_RESET etc)."""
    import time

    for attempt in range(1, max_retries + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(3000)
            if wait_selector:
                page.wait_for_selector(wait_selector, timeout=timeout_ms)
            return page.content()
        except Exception as e:
            if attempt < max_retries:
                delay = min(2 * attempt, 60)
                time.sleep(delay)
            else:
                raise


def fetch_hltv_page_html(
    url: str,
    *,
    wait_selector: str = 'a[href*="/matches/"]',
    headless: bool = False,
    profile_dir: Path | None = None,
    timeout_ms: int = 60_000,
) -> str:
    """Fetch an HLTV page HTML via CDP. Auto-launches Chrome if needed."""
    browser = _ensure_cdp_browser()
    ctx = browser.new_context()
    try:
        page = ctx.new_page()
        return _navigate_with_retry(
            page, url, wait_selector=wait_selector, timeout_ms=timeout_ms
        )
    finally:
        ctx.close()


def match_slug_from_url(url: str) -> str:
    """Match folder slug from HLTV URL (strips tournament suffix and trailing year)."""
    m = re.search(r"/matches/\d+/([^/?#]+)", url)
    if not m:
        raise ValueError(f"Could not extract match slug from URL: {url}")
    path = m.group(1)
    path = re.sub(r"-cs-.*", "", path, flags=re.IGNORECASE)
    path = re.sub(r"-\d{4}$", "", path)
    return path


def match_id_from_url(url: str) -> str:
    m = re.search(r"/matches/(\d+)/", url)
    return m.group(1) if m else ""


def match_demo_dir(slug: str) -> Path:
    return settings.hltv_demo_dir / slug


def _match_info(url: str) -> MatchInfo:
    slug = match_slug_from_url(url)
    parts = slug.split("-vs-", 1)
    team1 = parts[0].replace("-", " ").title() if parts else slug
    team2 = parts[1].replace("-", " ").title() if len(parts) > 1 else ""
    return MatchInfo(
        match_id=match_id_from_url(url),
        source=DemoSource.HLTV,
        url=url,
        team1=team1,
        team2=team2,
    )


def find_valid_archive(folder: Path) -> Path | None:
    if not folder.is_dir():
        return None
    best: Path | None = None
    for p in folder.iterdir():
        if p.suffix.lower() not in ARCHIVE_EXTENSIONS:
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size >= MIN_ARCHIVE_BYTES and (best is None or size > best.stat().st_size):
            best = p
    return best


def remove_invalid_archives(folder: Path) -> None:
    if not folder.is_dir():
        return
    for p in folder.iterdir():
        if p.suffix.lower() not in ARCHIVE_EXTENSIONS:
            continue
        try:
            if p.stat().st_size < MIN_ARCHIVE_BYTES:
                p.unlink()
                console.print(f"[yellow]   Removed undersized archive: {p.name}[/yellow]")
        except OSError:
            pass


def find_dem_for_map(folder: Path, map_name: str) -> Path | None:
    if not folder.is_dir():
        return None
    needle = map_name.lower()
    matches = sorted(
        (p for p in folder.glob("*.dem") if needle in p.name.lower()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return matches[0] if matches else None


def _kill_chrome_on_port_9222() -> None:
    """Kill any Chrome process listening on CDP port 9222 (frees profile lock)."""
    import subprocess, time
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "netstat -ano | findstr :9222"],
            capture_output=True, text=True, timeout=10,
        )
        for line in r.stdout.splitlines():
            if "LISTENING" in line:
                parts = line.strip().split()
                if len(parts) >= 5:
                    subprocess.run(["taskkill", "/F", "/PID", parts[-1]],
                                   capture_output=True, timeout=5)
        time.sleep(1)
    except Exception:
        pass


def _download_with_browser(
    match_url: str,
    dest_path: Path,
    profile_dir: Path,
    *,
    headless: bool,
    max_retries: int = 10,
) -> Path:
    """Download demo via CloakBrowser. Retry with random delay if error page appears."""
    from cloakbrowser import launch_persistent_context
    import random
    import time

    profile_dir.mkdir(parents=True, exist_ok=True)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    ctx = launch_persistent_context(
        str(profile_dir.resolve()),
        headless=headless,
        viewport={"width": 1920, "height": 1080},
        humanize=True,
        channel="chrome",
    )

    for attempt in range(1, max_retries + 1):
        page = ctx.new_page()
        try:
            page.goto(match_url)
            page.wait_for_selector(
                "[data-demo-link], [data-demo-link-button]",
                state="visible",
                timeout=30_000,
            )
            console.print(f"  [cyan]Clicking 'Demo download' (attempt {attempt}/{max_retries})...[/cyan]")
            with page.expect_download(timeout=120_000) as dl_info:
                page.locator("text=Demo download").first.click(force=True)
            dl = dl_info.value
            ext = Path(dl.suggested_filename).suffix or ".rar"
            if dest_path.suffix.lower() not in ARCHIVE_EXTENSIONS:
                dest_path = dest_path.with_suffix(ext)
            dl.save_as(str(dest_path))
            dl = None
            try:
                ctx.close()
            except Exception:
                pass
            time.sleep(0.5)
            console.print(f"  [green]Downloaded: {dest_path.name} ({dest_path.stat().st_size / 1024 / 1024:.0f} MB)[/green]")
            return dest_path
        except Exception as e:
            console.print(f"  [yellow]Attempt {attempt} failed: {e}[/yellow]")
            try:
                page.close()
            except Exception:
                pass
            if attempt < max_retries:
                wait = random.uniform(4, 8)
                console.print(f"  [yellow]Retrying in {wait:.0f}s...[/yellow]")
                time.sleep(wait)

    try:
        ctx.close()
    except Exception:
        pass
    raise RuntimeError(f"Download failed after {max_retries} attempts")


def _ensure_extracted(archive_path: Path, folder: Path) -> list[Path]:
    existing = list(folder.glob("*.dem"))
    if existing:
        return existing
    console.print(f"[cyan]   [EXTRACT] {archive_path.name} -> {folder.name}/[/cyan]")
    return extract_demo(archive_path, folder)


def resolve_demo_for_pov(
    match_url: str,
    map_name: str,
    *,
    demo_override: Path | None = None,
    force: bool = False,
    headless: bool = False,
    profile_dir: Path | None = None,
) -> Path:
    """Resolve the .dem path for a POV: download (if needed), extract, pick map."""
    profile = profile_dir or DEFAULT_PROFILE_DIR
    slug = match_slug_from_url(match_url)
    folder = match_demo_dir(slug)
    folder.mkdir(parents=True, exist_ok=True)

    if demo_override is not None:
        src = demo_override.resolve()
        if not src.exists():
            raise FileNotFoundError(f"Demo override not found: {src}")
        if src.suffix.lower() == ".dem":
            return src
        if src.suffix.lower() in ARCHIVE_EXTENSIONS:
            _ensure_extracted(src, folder)
            dem = find_dem_for_map(folder, map_name)
            if dem is None:
                raise ValueError(f"No .dem for map '{map_name}' after extracting {src.name}")
            return dem
        raise ValueError(f"Unsupported demo override: {src.suffix}")

    dem = find_dem_for_map(folder, map_name)
    if dem is not None and not force:
        console.print(f"[green]   [OK] Using existing demo: {dem.name}[/green]")
        return dem

    remove_invalid_archives(folder)
    archive = find_valid_archive(folder)

    if force and archive is not None:
        archive.unlink()
        archive = None

    if archive is None:
        console.print(f"[cyan]   [DL] Downloading match archive via CloakBrowser...[/cyan]")
        dest = folder / slug  # extension filled by browser
        archive = _download_with_browser(match_url, dest, profile, headless=headless)
        if archive.stat().st_size < MIN_ARCHIVE_BYTES:
            archive.unlink(missing_ok=True)
            raise ValueError("Downloaded archive is too small (incomplete download)")

    _ensure_extracted(archive, folder)

    dem = find_dem_for_map(folder, map_name)
    if dem is None:
        available = ", ".join(p.name for p in sorted(folder.glob("*.dem")))
        raise ValueError(
            f"No .dem for map '{map_name}' in {folder}. Available: {available or '(none)'}"
        )
    return dem


def acquire_match(
    match_url: str,
    *,
    force: bool = False,
    headless: bool = False,
    profile_dir: Path | None = None,
) -> DownloadResult:
    """Download and extract all demos for a match (no map filter)."""
    started = datetime.now()
    match_info = _match_info(match_url)
    profile = profile_dir or DEFAULT_PROFILE_DIR
    slug = match_slug_from_url(match_url)
    folder = match_demo_dir(slug)
    folder.mkdir(parents=True, exist_ok=True)

    try:
        console.print(f"\n[bold cyan][>>] Acquiring:[/bold cyan] {match_url}")

        existing = is_already_downloaded(match_info.match_id, DemoSource.HLTV)
        if existing and existing.exists() and not force:
            console.print(f"[yellow]   [SKIP] Already in history: {existing}[/yellow]")
            return DownloadResult(
                match=match_info,
                status=DownloadStatus.SKIPPED,
                demo_path=existing,
                file_size_mb=file_size_mb(existing),
                started_at=started,
                completed_at=datetime.now(),
            )

        remove_invalid_archives(folder)
        archive = find_valid_archive(folder)

        if force and archive is not None:
            archive.unlink()
            archive = None

        if archive is None:
            console.print("[cyan]   [DL] Downloading via CloakBrowser...[/cyan]")
            dest = folder / slug
            archive = _download_with_browser(match_url, dest, profile, headless=headless)
            if archive.stat().st_size < MIN_ARCHIVE_BYTES:
                archive.unlink(missing_ok=True)
                raise ValueError("Downloaded archive is too small (incomplete download)")
        else:
            console.print(f"[green]   [OK] Using archive: {archive.name}[/green]")

        dem_paths = _ensure_extracted(archive, folder)
        demo_path = dem_paths[0] if dem_paths else archive

        result = DownloadResult(
            match=match_info,
            status=DownloadStatus.COMPLETED,
            demo_path=demo_path,
            file_size_mb=file_size_mb(archive),
            started_at=started,
            completed_at=datetime.now(),
        )
        record_download(result)
        console.print(
            f"[bold green]   [DONE] {folder} ({len(dem_paths)} .dem file(s))[/bold green]"
        )
        return result

    except Exception as e:
        console.print(f"[bold red]   [ERR] {e}[/bold red]")
        return DownloadResult(
            match=match_info,
            status=DownloadStatus.FAILED,
            error=str(e),
            started_at=started,
            completed_at=datetime.now(),
        )
