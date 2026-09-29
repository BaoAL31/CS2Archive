"""Single source of truth for the renders/ layout.

::

    renders/
      pov-{demo_stem}_{nick}/   per-POV home (pipeline render dir)
        hook/                   hook_timeline.json, hook_render.json,
                                segments/, hook.mp4
        intro/                  intro.png, intro_details.json, footage/
        shorts/                 shorts-{slug}/short_timeline.json (+ meta, mp4)
        action_timeline.json    per-demo cache, pov-local copy (hook needs
                                victim weapons + duel/opener/trade moments;
                                thumbnail needs kills + round stakes)
        combined.mp4, round clips, sidecars, utility_cams/, .overlay_work/
      hl-{demo_stem}/           MULTI-PROS highlights pipeline only — the POV
                                flow never creates this (no auto-generation).
      stat-strips/ voice-review/ hlae-diagnostic-*/
                                shared/miscellany, out of scope.

Shorts live under the SHORT'S POV folder (``pov-{stem}_{nick}/shorts/``),
resolved from backlog cards (exact pipeline nick strings — never the demo's
raw name). Anything that scans shorts must glob; nothing may hardcode
``renders/shorts/`` anymore (kept as a legacy fallback read path only).
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RENDERS_DIR = PROJECT_ROOT / "renders"

# Subdirs of a pov dir that are deliverables or cross-step caches: the
# post-upload purge and the queue-clean sweep must spare these.
POV_KEEP_DIRS = ("shorts",)


def run_id_from_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:80].strip("_")


def pov_dir(dem_stem: str, player: str) -> Path:
    """Canonical per-POV home: renders/pov-{stem}_{nick-slug}."""
    return RENDERS_DIR / f"pov-{dem_stem}_{run_id_from_name(player)}"


def hook_dir(pov: str | Path) -> Path:
    return Path(pov) / "hook"


def intro_dir(pov: str | Path) -> Path:
    return Path(pov) / "intro"


def shorts_base(pov: str | Path) -> Path:
    return Path(pov) / "shorts"


def strips_base(pov: str | Path) -> Path:
    """Match stat-strips root inside a POV folder: {pov}/stat-strips."""
    return Path(pov) / "stat-strips"


def pov_action_timeline(pov: str | Path) -> Path:
    return Path(pov) / "action_timeline.json"


def hl_dir(dem_stem: str) -> Path:
    """Multi-pros highlights home. POV flow must not create this."""
    return RENDERS_DIR / f"hl-{dem_stem}"


def find_pov_dirs(dem_stem: str) -> list[Path]:
    """Every per-POV dir for a demo stem (nick varies)."""
    if not RENDERS_DIR.is_dir():
        return []
    return sorted(
        (p for p in RENDERS_DIR.glob(f"pov-{dem_stem}_*") if p.is_dir()),
        key=lambda p: p.name,
    )


def find_action_timeline(dem_stem: str) -> Path | None:
    """POV-local cache first, multi-pros hl- cache second, else None."""
    for pov in find_pov_dirs(dem_stem):
        cand = pov_action_timeline(pov)
        if cand.is_file():
            return cand
    legacy = hl_dir(dem_stem) / "action_timeline.json"
    if legacy.is_file():
        return legacy
    return None


def find_match_strips(match_id: str) -> list[Path]:
    """Dirs holding a captured repeek pane pair for a match, anywhere.

    POV-local copies first, legacy shared tree last. The intro builder
    copies from the first hit instead of re-capturing.
    """
    out: list[Path] = []
    if RENDERS_DIR.is_dir():
        for pov in sorted(RENDERS_DIR.glob("pov-*")):
            if not pov.is_dir():
                continue
            cand = pov / "stat-strips" / match_id
            if ((cand / "repeek_left.png").is_file()
                    and (cand / "repeek_right.png").is_file()
                    and cand not in out):
                out.append(cand)
    legacy = RENDERS_DIR / "stat-strips" / match_id
    if ((legacy / "repeek_left.png").is_file()
            and (legacy / "repeek_right.png").is_file()
            and legacy not in out):
        out.append(legacy)
    return out


def find_short_timelines(dem_stem: str) -> list[Path]:
    """All short_timeline.json for a demo stem, new layout + legacy."""
    out: list[Path] = []
    for pov in find_pov_dirs(dem_stem):
        base = shorts_base(pov)
        if base.is_dir():
            out.extend(sorted(base.glob("shorts-*/short_timeline.json")))
    legacy = RENDERS_DIR / "shorts" / f"shorts-{dem_stem}"
    if legacy.is_dir():
        out.extend(sorted(legacy.glob("shorts-*/short_timeline.json")))
    return out


def purge_pov_dir(pov: str | Path, *, dry_run: bool = False) -> float:
    """Delete a POV render dir except deliverable subdirs (shorts/).

    Returns GB freed. Shared by the post-upload purge, the listener's
    dup-clean sweep and the manual cleanup step — shorts are a separate
    upload flow and must survive the long-form purge.
    """
    rd = Path(pov)
    try:
        freed = sum(f.stat().st_size for f in rd.rglob("*") if f.is_file())
    except OSError:
        return 0.0
    if dry_run:
        return freed / 1e9
    for child in sorted(rd.iterdir()):
        if child.name in POV_KEEP_DIRS:
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                child.unlink()
            except OSError:
                pass
    try:
        if not any(rd.iterdir()):
            rd.rmdir()
    except OSError:
        pass
    return freed / 1e9
