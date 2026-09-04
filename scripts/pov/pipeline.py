"""
E2E pipeline: backlog metadata -> analyze -> render -> concat -> overlay -> outro -> thumbnail.
Reads all POV metadata from a backlog file.
Structured errors for agent parsing.
Auto-downloads missing demos from HuggingFace (if hf_root set) or falls back to CloakBrowser (if hltv_url set).

Usage:
    python scripts/pov/pipeline.py --backlog backlog/<match_slug>/<priority>/<slug>.json [--step N] [--raw-only]

    Steps (use --step N to start at a specific step):
  1 = analyze    csdm analyze the demo
  2 = render     Render all rounds as POV clips
  3 = concat     Concatenate rounds; raw-only copies combined to youtube/
  4 = overlay    Keyboard + util cam (default product; skipped with --raw-only)
  5 = outro      Generate 5s silent outro, concat onto video.mp4
  6 = thumbnail  Generate 1280x720 thumbnail + write upload_meta.json
  7 = cleanup    Remove renders folder + pipeline state

NOTE: Uploading is NOT done by the pipeline. The pipeline only produces the
finished video, thumbnail, and upload_meta.json (title/description/tags/
thumbnail_path/youtube_id=None/upload_status="pending"). A separate script,
scripts/upload/upload_pending.py, scans every upload_meta.json under youtube/ and
uploads any that are still pending.

Default product is one overlay variant at youtube/{run_id}_overlay/.
Pass --raw-only for youtube/{run_id}/ with no overlay.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
import time
from pathlib import Path

# scripts/ on path so _pathsetup + overlay package resolve
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _pathsetup import ensure, SCRIPTS_DIR

PROJECT_ROOT = ensure()

from assign_playlist import normalize_playlist_name
from config import settings, apply_runtime_env

apply_runtime_env()
from huggingface_hub import hf_hub_download
STATE_DIR = PROJECT_ROOT / ".pipeline"
STATE_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(str(PROJECT_ROOT))

CSDM = settings.csdm_cmd
PY = sys.executable

STEPS = {
    1: "analyze",
    2: "render",
    3: "concat",
    4: "overlay",
    5: "outro",
    6: "thumbnail",
    7: "cleanup",
}

REQUIRED_META_FIELDS = [
    "player",
    "map",
    "hltv_url",
    "steam_id",
    "demo_path",
    "ratings_path",
    "tournament",
]


def _tournament_logo_path(tournament: str, hf_root: str) -> Path | None:
    """Return a logo image path for known tournaments (e.g. EWC 2026), else None."""
    t = (tournament or "").lower()
    h = (hf_root or "").lower()
    if "esports world cup" in t or h == "esports_world_cup_2026" or "ewc" in h:
        p = PROJECT_ROOT / "assets" / "tournaments" / "ewc_2026.png"
        if p.exists():
            return p
    return None


def pipeline_error(step: int, code: str, message: str) -> str:
    payload = json.dumps({
        "error": True,
        "step": step,
        "step_name": STEPS.get(step, "unknown"),
        "code": code,
        "message": message,
    })
    return f"[PIPELINE_ERROR] {payload}"


def fail(step: int, code: str, message: str) -> None:
    print(pipeline_error(step, code, message))
    sys.exit(1)


def load_state(run_id: str) -> dict:
    path = STATE_DIR / f"{run_id}.json"
    if path.exists():
        return json.loads(path.read_text())
    return {"step": 1, "data": {}}


def save_state(run_id: str, state: dict) -> None:
    (STATE_DIR / f"{run_id}.json").write_text(json.dumps(state, indent=2))


def _minimize_cs2() -> None:
    """Load CS2Archive's minimizer, not CS2UtilArchive's shadowed module."""
    import importlib.util
    path = Path(__file__).resolve().parents[1] / "cs2_minimizer.py"
    spec = importlib.util.spec_from_file_location("_cs2archive_cs2_minimizer", path)
    if spec is None or spec.loader is None:
        return
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.ensure_cs2_closed()


def run_id_from_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:80].strip("_")


def pov_render_dir(dem_stem: str, player: str) -> Path:
    player_slug = run_id_from_name(player)
    return PROJECT_ROOT / "renders" / f"pov-{dem_stem}_{player_slug}"


def _faceit_rename_map(demo_path: Path) -> dict:
    """Build {SteamID64 -> canonical pro name} for recognized pros in a FACEIT demo.

    Players often change their in-game FACEIT name to avoid recognition, so the
    rendered HUD shows each recognized pro's canonical name instead of the demo's
    recorded (possibly disguised) name. Only pros we actually recognize are
    renamed; everyone else keeps their recorded name.
    """
    from scripts.faceit.faceit_names import known_pro_steam_ids

    if not demo_path or not demo_path.exists():
        return {}
    try:
        import demoparser2 as dp
        info = dp.DemoParser(str(demo_path)).parse_player_info()
        steam_ids = {str(r.get("steamid", "")).strip() for _, r in info.iterrows()}
    except Exception:
        return {}
    pro_names = known_pro_steam_ids()  # steam_id_64 -> canonical pro name
    rename_map = {}
    for sid in steam_ids:
        name = pro_names.get(sid)
        if name:
            rename_map[sid] = name
    return rename_map


# FACEIT POVs enable voice comms by default ONLY when the demo has enough
# real voice to be worth it (shade + comms mix). Below this many seconds of
# POV-team voice, default stays OFF to avoid mixing near-empty comms.
VOICE_AUTO_MIN_TEAM_SECONDS = 180.0  # ~3 min of team voice


def _faceit_voice_enabled(demo_path: Path, steam_id: str) -> bool:
    """Whether a FACEIT demo warrants voice by default (enough team voice).

    Auto-detection: enable voice only if the POV player's team has at least
    VOICE_AUTO_MIN_TEAM_SECONDS of recorded voice. Absent/unknown → False so we
    never default into mixing a demo that has no real comms.
    """
    if not demo_path or not demo_path.exists():
        return False
    try:
        from scripts.faceit.mix_team_voice import pov_team_voice_seconds
        secs = pov_team_voice_seconds(demo_path, steam_id)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            "FACEIT voice comms required, but voice detection failed: "
            f"{type(e).__name__}: {e}"
        ) from e
    return secs >= VOICE_AUTO_MIN_TEAM_SECONDS


def _faceit_kd_from_demo(demo_path: Path, steam_id: str) -> tuple[int, int] | None:
    """(kills, deaths) for the POV player from the demo's player_death events.

    Fallback used when the backlog card lacks explicit kills/deaths (older match
    cards only stored the `kd` ratio). Matches csdm's convention: suicides
    (attacker == victim) and the knife round are excluded. None when unavailable
    or the player had zero deaths.
    """
    try:
        import demoparser2 as dp
        parser = dp.DemoParser(str(demo_path))
        deaths = parser.parse_event("player_death")
        round_starts = parser.parse_event("round_start")
    except Exception:
        return None
    if deaths is None or len(deaths) == 0:
        return None
    first_real_tick = 0
    if round_starts is not None and len(round_starts):
        r1 = round_starts[round_starts["round"] == 1]
        if len(r1):
            first_real_tick = int(r1["tick"].max())
    att = deaths["attacker_steamid"].astype(str)
    vic = deaths["user_steamid"].astype(str)
    core = deaths[(deaths["tick"] >= first_real_tick) & (att != vic)]
    kills = int((core["attacker_steamid"].astype(str) == steam_id).sum())
    deaths_n = int((core["user_steamid"].astype(str) == steam_id).sum())
    if deaths_n == 0:
        return None
    return kills, deaths_n


def _parse_backlog(path: str) -> dict:
    """Parse backlog .json file and validate required fields."""
    p = Path(path)
    if not p.exists():
        fail(0, "BACKLOG_NOT_FOUND", f"Backlog file not found: {p}")

    if p.suffix.lower() not in (".json",):
        fail(0, "BACKLOG_BAD_FORMAT", f"Backlog must be .json, got: {p.suffix}")

    try:
        meta = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        fail(0, "BACKLOG_PARSE_ERROR", f"Failed to parse {path}: {e}")

    missing = [f for f in REQUIRED_META_FIELDS if not meta.get(f)]
    # FACEIT matches have no HLTV url/ratings — those fields are optional there.
    if meta.get("is_faceit") or str(meta.get("demo_path", "")).replace("\\", "/").count("demos/faceit"):
        missing = [f for f in missing if f not in ("hltv_url", "ratings_path", "tournament")]
    if missing:
        fail(0, "BACKLOG_MISSING_FIELDS",
             f"Backlog missing required fields: {', '.join(missing)}")

    return meta


class Pipeline:
    def __init__(self, args):
        self.args = args
        self.meta = _parse_backlog(args.backlog)
        self._ensure_video_settings()

        self.player = self.meta["player"]
        self.map_name = self.meta["map"]
        self.hltv_url = self.meta.get("hltv_url", "")
        self.steam_id = self.meta.get("steam_id", "")
        self.tournament = self.meta.get("tournament", "")
        self.hf_root = self.meta.get("hf_root", "").strip()

        demo_path_str = self.meta.get("demo_path", "")
        self.demo_path = Path(demo_path_str) if demo_path_str else None

        self.is_faceit = bool(self.meta.get("is_faceit")) or bool(
            self.demo_path and "demos/faceit" in str(self.demo_path).replace("\\", "/")
        )
        self._voice_cache: bool | None = None

        ratings_path_str = self.meta.get("ratings_path", "")
        self.ratings_json = Path(ratings_path_str) if ratings_path_str else PROJECT_ROOT / "demos" / "analysis" / ""

        avatar_path_str = self.meta.get("avatar_path", "")
        self.avatar_path = Path(avatar_path_str) if avatar_path_str else None

        self.start_step = args.step if args.step is not None else 1
        self._cli_step = args.step  # None = no explicit --step → allow auto-skip
        self.end_step = args.until if args.until is not None else max(STEPS.keys())
        # Default is no-cleanup (skip step 7) unless --cleanup is passed.
        # Default run therefore stops after thumbnail (step 6), producing the
        # finished video + upload_meta.json but NOT uploading or cleaning up.
        if getattr(args, "no_cleanup", True) and self.end_step == max(STEPS.keys()):
            self.end_step = max(STEPS.keys()) - 1

        from scrapers.hltv_acquire import match_id_from_url
        from variant import resolve_skip_overlay, youtube_dir_name

        if self.is_faceit:
            # FACEIT has no HLTV url — derive match id from demo stem / meta.
            self.match_id = self.meta.get("faceit_match_id") or (
                self.demo_path.stem.split(" - ")[0] if self.demo_path else "faceit"
            )
            slug = self.match_id
        else:
            slug = self.hltv_url.rstrip("/").split("/")[-1]
            self.match_id = match_id_from_url(self.hltv_url)
        dem_stem = self.demo_path.stem if self.demo_path else slug
        self.render_dir = pov_render_dir(dem_stem, self.player)
        self.run_id = run_id_from_name(f"{self.match_id}_{dem_stem}_{self.player}_{self.map_name}")
        self.state = load_state(self.run_id)
        self.state.setdefault("data", {})
        self.state["data"]["steam_id"] = self.steam_id

        raw_only = bool(getattr(args, "raw_only", False))
        self.skip_overlay = resolve_skip_overlay(raw_only=raw_only, state=self.state)
        yt_name = youtube_dir_name(self.run_id, skip_overlay=self.skip_overlay)
        self.youtube_dir = PROJECT_ROOT / "youtube" / yt_name

        if self.state["data"].get("demo_path"):
            self.demo_path = Path(self.state["data"]["demo_path"])
        self.state["data"]["render_dir"] = str(self.render_dir)
        self.state["data"]["youtube_dir"] = str(self.youtube_dir)
        self.state["data"]["ratings_path"] = str(self.ratings_json)
        self.state["data"]["skip_overlay"] = self.skip_overlay
        if self.avatar_path:
            self.state["data"]["avatar_path"] = str(self.avatar_path)

    def _ensure_video_settings(self) -> None:
        """Fill capture dims + viewmodel from prosettings when backlog lacks them."""
        need_capture = not (
            int(self.meta.get("capture_width") or 0) >= 800
            and int(self.meta.get("capture_height") or 0) >= 600
        )
        need_vm = self.meta.get("viewmodel_fov") is None
        if not need_capture and not need_vm:
            return
        try:
            from scrapers.prosettings import backlog_video_fields
            fields = backlog_video_fields(self.meta.get("player", ""))
            if need_capture:
                for k in (
                    "resolution", "aspect_ratio", "scaling_mode",
                    "capture_width", "capture_height", "video_settings_source",
                ):
                    if k in fields:
                        self.meta[k] = fields[k]
                print(f"  [video] {fields['capture_width']}x{fields['capture_height']} "
                      f"{fields.get('aspect_ratio', '')} {fields.get('scaling_mode', '')} "
                      f"(source={fields.get('video_settings_source', '?')})")
            if need_vm:
                for k in (
                    "viewmodel_fov", "viewmodel_offset_x", "viewmodel_offset_y",
                    "viewmodel_offset_z", "viewmodel_presetpos",
                ):
                    if fields.get(k) is not None:
                        self.meta[k] = fields[k]
                if self.meta.get("viewmodel_fov") is not None:
                    print(f"  [viewmodel] fov={self.meta.get('viewmodel_fov')} "
                          f"xyz=({self.meta.get('viewmodel_offset_x')},"
                          f"{self.meta.get('viewmodel_offset_y')},"
                          f"{self.meta.get('viewmodel_offset_z')})")
        except Exception as e:
            print(f"  [WARN] video settings lookup failed: {e}")

    def _ensure_demo(self) -> None:
        if self.demo_path and self.demo_path.exists():
            return
        if not self.demo_path:
            return

        from scrapers.hltv_acquire import acquire_match
        from models import DownloadStatus

        hf_root = self.meta.get("hf_root", "").strip()
        hf_repo = self.meta.get("hf_repo", "cs2povarchive/cs2-demos")

        if hf_root:
            match_slug = self.demo_path.parent.name
            dem_filename = self.demo_path.name
            hf_folder = f"{self.match_id}-{match_slug}" if self.match_id else match_slug
            hf_remote = f"{hf_root}/{hf_folder}/{dem_filename}"
            self.demo_path.parent.mkdir(parents=True, exist_ok=True)
            print(f"  [HF] Demo not found locally. Downloading from {hf_repo}...")
            print(f"       hf://{hf_repo}/{hf_remote}")
            try:
                cached = hf_hub_download(
                    repo_id=hf_repo,
                    filename=hf_remote,
                    repo_type="dataset",
                )
                shutil.copy2(cached, self.demo_path)
                if self.demo_path.exists():
                    mb = self.demo_path.stat().st_size / 1024 / 1024
                    rel = self.demo_path.relative_to(PROJECT_ROOT) if self.demo_path.is_absolute() else self.demo_path
                    print(f"  [OK] Demo downloaded ({mb:.0f} MB): {rel}")
                    return
            except Exception as e:
                print(f"  [WARN] HF download failed: {e}")

        if not self.hltv_url:
            fail(0, "DEMO_NOT_FOUND",
                 f"Demo not found ({self.demo_path}) and no hltv_url or hf_root to download from")

        print(f"  [DL] Falling back to CloakBrowser: {self.hltv_url}")
        try:
            result = acquire_match(self.hltv_url)
            if result.status != DownloadStatus.COMPLETED:
                fail(0, "DEMO_DOWNLOAD_CLOAK_FAILED",
                     f"CloakBrowser download failed: {result.error or 'unknown'}")
        except Exception as e:
            fail(0, "DEMO_DOWNLOAD_CLOAK_ERROR",
                 f"CloakBrowser error: {e}")

        if not self.demo_path.exists():
            fail(0, "DEMO_NOT_FOUND",
                 f"Download complete but demo still not found: {self.demo_path}")
        mb = self.demo_path.stat().st_size / 1024 / 1024
        rel = self.demo_path.relative_to(PROJECT_ROOT) if self.demo_path.is_absolute() else self.demo_path
        print(f"  [OK] Demo downloaded ({mb:.0f} MB): {rel}")

    def _auto_skip_completed(self) -> None:
        """Bump start_step past steps whose output artifacts already exist on disk.

        Lets `pipeline.py` re-runs skip re-rendering when combined.mp4 etc.
        are already there. Honors --step (CLI override always wins).
        Only applies to steps 1-3 (render-side artifacts); steps 4+ have their
        own resume logic.
        """
        # If user explicitly passed --step, don't auto-bump
        cli_step = getattr(self, "_cli_step", None)
        if cli_step is not None:
            return
        # Already saved state takes precedence
        if self.state.get("step", 0) > self.start_step:
            self.start_step = max(self.start_step, self.state["step"])
        # Step 1: analyze — csdm "already in database" is fast, skip only if
        # rounds count in state matches what's on disk. Cheap re-run otherwise.
        # Step 2: render — combined.mp4 in render_dir means all batches done
        render_dir = self.render_dir
        combined = render_dir / "combined.mp4"
        if combined.is_file() and combined.stat().st_size > 100 * 1024 * 1024:
            # combined.mp4 only exists after step 3 (concat) has run, so step 2
            # (render) is complete. Step 3 (concat/scale) is ONLY complete if
            # combined.mp4 is at the target export resolution (2560x1440).
            # If a scale encode aborts mid-way, combined.mp4 is left at the
            # NATIVE render resolution — treating that as "step 3 done" would
            # skip the scale and ship an unscaled (e.g. 1280x960) video.
            from concat_rounds import _get_resolution

            FINAL_W = int(getattr(self.args, "width", None) or 2560)
            FINAL_H = int(getattr(self.args, "height", None) or 1440)
            w, h = _get_resolution(combined)
            if (w, h) == (FINAL_W, FINAL_H):
                print(f"  [skip] render + scale complete: {combined.name} "
                      f"({combined.stat().st_size // 1024 // 1024} MB, {w}x{h})")
                if self.start_step <= 3:
                    self.start_step = 4
                    self.state["step"] = 4
                    save_state(self.run_id, self.state)
            else:
                # Scale incomplete — force step 3 (concat/scale) to re-run,
                # even if stale state claimed a later step. Step 2 stays
                # skipped (combined exists => render done).
                print(f"  [resume] combined.mp4 is {w}x{h} (native, not scaled to "
                      f"{FINAL_W}x{FINAL_H}) -- step 3 (concat/scale) will re-run")
                if self.start_step != 3:
                    self.start_step = 3
            return
        # Step 2 partial: any batch-*.mp4 ≥1MB exists → skip analyze (cheap
        # re-analyze is fine) but DO NOT skip render (filesystem-based resume
        # inside render_pov.py will pick up existing batches).
        # Step 3: no combined.mp4 yet → keep at user's start_step

    def _voice_enabled(self) -> bool:
        """Whether voice comms (shade + comms mix) should run for this card.

        Policy: FACEIT POVs always carry team voice comms (always on);
        HLTV POVs never do (always off). Explicit flags can still force it on.
        """
        if getattr(self.args, "enable_voice_comms", False) or getattr(
            self.args, "voice_shade", False
        ):
            return True
        if not self.is_faceit:
            return False
        # FACEIT defaults ON only when the demo actually carries enough
        # POV-team voice (>= VOICE_AUTO_MIN_TEAM_SECONDS). Many FACEIT demos
        # record zero team voice packets — mixing those would refuse with
        # VOICE_COMMS_FAILED rather than emit a silent 'comms' result.
        return _faceit_voice_enabled(self.demo_path, self.steam_id)

    def _setup_logging(self) -> Path:
        """Redirect all output (Python prints + subprocess stdout/stderr) to
        ``logs/{self.run_id}.log`` so harness stdout truncation can't kill a
        long pipeline. Returns the log path. Call after printing the path to
        original stdout."""
        log_path = PROJECT_ROOT / "logs" / f"{self.run_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # Fresh start marker
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"Pipeline {self.run_id} starting at {ts}\n")
            f.write(f"{'='*60}\n")
        # Redirect at OS level so subprocess output also goes to the log.
        try:
            log_fd = os.open(str(log_path), os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o644)
            os.dup2(log_fd, sys.stdout.fileno())
            os.dup2(log_fd, sys.stderr.fileno())
            os.close(log_fd)
            # Re-wrap Python stdout/stderr on the redirected FDs
            sys.stdout = open(1, "w", encoding="utf-8", closefd=False)
            sys.stderr = open(2, "w", encoding="utf-8", closefd=False)
        except Exception as exc:
            # Non-fatal: if redirection fails, continue with original stdout
            print(f"  [warn] failed to set up log redirection to {log_path}: {exc}")
        return log_path

    def run(self) -> None:
        log_path = PROJECT_ROOT / "logs" / f"{self.run_id}.log"
        print(f"Pipeline log -> {log_path.resolve()}")
        self._setup_logging()
        if self.end_step < self.start_step:
            fail(0, "INVALID_STEP_RANGE", f"--until {self.end_step} is before --step {self.start_step}")
        self._ensure_demo()
        self._auto_skip_completed()
        for step_num in range(self.start_step, self.end_step + 1):
            step_name = STEPS[step_num]
            print(f"\n{'='*60}")
            print(f"  Step {step_num}/{max(STEPS.keys())}: {step_name}")
            print(f"{'='*60}")
            try:
                getattr(self, f"step_{step_name}")()
                self.state["step"] = step_num + 1
                save_state(self.run_id, self.state)
            except Exception as e:
                # Include traceback so the failing file/line is visible in the
                # log — str(e) alone often hides WHERE the step blew up.
                tb = traceback.format_exc()
                print(tb)
                fail(step_num, f"STEP_{step_name.upper()}_EXCEPTION",
                     f"{e} | {tb.strip().splitlines()[-1] if tb else ''}")

        print(f"\n  [OK] Pipeline complete -> {self.youtube_dir}/")
        # Video is youtube-ready (thumbnail + upload_meta written): the render
        # folder is pure dead weight now — purge it unless opted out.
        print("\n  [cleanup] purging render intermediates...")
        try:
            self._purge_render_intermediates(full=True)
        except Exception as e:
            print(f"  [WARN] post-run cleanup failed (non-fatal): {e}")

    def _run_py(self, args: list[str], **kwargs):
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        kwargs.setdefault("env", env)
        if kwargs.get("text") and "encoding" not in kwargs:
            kwargs["encoding"] = "utf-8"
        return subprocess.run([PY] + args, **kwargs)

    # ── Step 1: Analyze ──────────────────────────────────────────────────

    def step_analyze(self) -> None:
        if not self.demo_path or not self.demo_path.exists():
            fail(1, "ANALYZE_DEMO_MISSING", f"demo not found: {self.demo_path}")

        cmd = [CSDM, "analyze", str(self.demo_path)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        combined = (r.stdout or "") + (r.stderr or "")

        if "unknown demo source" in combined.lower():
            cmd += ["--source", "challengermode"]
            print("  [PGL] Retrying with --source challengermode...")
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            combined = (r.stdout or "") + (r.stderr or "")

        if "already in database" in combined:
            print("  [OK] Already analyzed")
        elif r.returncode == 0:
            print("  [OK] Analysis done")
        else:
            fail(1, "ANALYZE_FAILED", f"csdm analyze returned {r.returncode}: {combined[:300]}")

        with tempfile.TemporaryDirectory() as tmp:
            r2 = subprocess.run([CSDM, "json", str(self.demo_path), "--output-folder", tmp],
                                capture_output=True, text=True, timeout=300)
            if r2.returncode != 0:
                fail(1, "ANALYZE_JSON_FAILED", f"csdm json export failed: {r2.stderr[:200]}")
            jf = list(Path(tmp).glob("*.json"))
            if not jf:
                fail(1, "ANALYZE_NO_JSON", "csdm json produced no output files")
            data = json.loads(jf[0].read_text(encoding="utf-8"))
            rounds = data.get("rounds", [])
            kills = data.get("kills", [])
            print(f"  [OK] Rounds: {len(rounds)}, Kills: {len(kills)}")
            if len(rounds) == 0:
                fail(1, "ANALYZE_NO_ROUNDS", "csdm analysis has zero rounds")
            self.state["data"]["round_count"] = len(rounds)
            tickrate = data.get("tickrate", 0)
            if tickrate:
                self.state["data"]["tickrate"] = tickrate
            # Persist the full csdm analysis json next to the render dir so
            # later steps (concat validation) can cross-check round count and
            # total tick span against the finished video without re-exporting.
            self.render_dir.mkdir(parents=True, exist_ok=True)
            analysis_path = self.render_dir / "csdm_analysis.json"
            analysis_path.write_text(json.dumps(data), encoding="utf-8")
            self.state["data"]["analysis_json"] = str(analysis_path)

    # ── Step 2: Render ───────────────────────────────────────────────────

    def step_render(self) -> None:
        if not self.demo_path or not self.demo_path.exists():
            fail(2, "RENDER_DEMO_MISSING", f"demo not found: {self.demo_path}")

        from render_version_check import RenderVersionError, assert_render_versions

        try:
            vers = assert_render_versions(self.demo_path)
            print(
                f"  [OK] versions demo={vers.get('demo')} cs2={vers.get('cs2')} "
                f"hlae={vers.get('hlae')} csdm={vers.get('csdm')}"
            )
        except RenderVersionError as e:
            fail(2, e.code, e.message)

        _minimize_cs2()

        nvcheck = subprocess.run(
            [
                settings.ffmpeg_exe,
                "-y", "-f", "lavfi", "-i", "color=c=red:s=2560x1440:d=1",
                "-c:v", "h264_nvenc", "-rc", "vbr_hq", "-b:v", "0", "-cq", "15",
                "-preset", "p7", "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=30,
        )
        if nvcheck.returncode != 0:
            fail(2, "RENDER_NO_NVENC",
                 f"h264_nvenc not available in ffmpeg. Install NVIDIA GPU drivers + NVENC-enabled ffmpeg.")

        steam_check = subprocess.run(["tasklist", "/FI", "IMAGENAME eq steam.exe"],
                                     capture_output=True, text=True, timeout=10)
        if "steam.exe" not in steam_check.stdout:
            fail(2, "RENDER_STEAM_NOT_RUNNING", "Steam must be running before rendering")

        skip_failed = getattr(self.args, "skip_failed_rounds", False)
        export_w = int(getattr(self.args, "width", None) or 0)
        export_h = int(getattr(self.args, "height", None) or 0)
        render_args = [
            "scripts/pov/render_pov.py", str(self.demo_path), self.steam_id,
            "--output", str(self.render_dir),
            "--batches", str(getattr(self.args, "batches", 1)),
            "--hook-timeout", str(getattr(self.args, "hook_timeout", 150.0)),
            "--hook-retries", str(getattr(self.args, "hook_retries", 2)),
        ]
        if skip_failed:
            render_args += ["--skip-failed-rounds"]
        cap_w = int(self.meta.get("capture_width") or 0)
        cap_h = int(self.meta.get("capture_height") or 0)
        if export_w >= 800 and export_h >= 600:
            render_args += ["--width", str(export_w), "--height", str(export_h)]
            print(f"  [capture] {export_w}x{export_h} (CLI --width/--height)")
        elif cap_w >= 800 and cap_h >= 600:
            render_args += ["--width", str(cap_w), "--height", str(cap_h)]
            print(f"  [capture] {cap_w}x{cap_h} "
                  f"({self.meta.get('aspect_ratio', '?')} "
                  f"{self.meta.get('scaling_mode', '')}) "
                  f"from {self.meta.get('video_settings_source', 'backlog')}")
        player = (self.meta.get("player") or "").strip()
        if player:
            render_args += ["--player", player]

        # Default for FACEIT: rename recognized pros in the rendered HUD to their
        # canonical pro name (players often change their FACEIT name to avoid
        # recognition, so we display the official pro name instead). Uses the same
        # canonical nickname logic as the title/thumbnail.
        if self.is_faceit and self.demo_path and self.demo_path.exists():
            rename_map = _faceit_rename_map(self.demo_path)
            if rename_map:
                render_args += ["--rename", json.dumps(rename_map)]
        for flag, key in (
            ("--viewmodel-fov", "viewmodel_fov"),
            ("--viewmodel-offset-x", "viewmodel_offset_x"),
            ("--viewmodel-offset-y", "viewmodel_offset_y"),
            ("--viewmodel-offset-z", "viewmodel_offset_z"),
            ("--viewmodel-presetpos", "viewmodel_presetpos"),
        ):
            val = self.meta.get(key)
            if val is not None and val != "":
                render_args += [flag, str(val)]
        hf_root = self.meta.get("hf_root", "").strip()
        if hf_root:
            render_args += ["--hf-root", hf_root]
            if self.match_id:
                render_args += ["--match-id", str(self.match_id)]
        r = self._run_py(render_args, timeout=43200)
        if r.returncode != 0:
            fail(2, "RENDER_FAILED", f"render_pov.py exited {r.returncode}")

        round_re = re.compile(r"^round-(\d+)-tick-\d+-to-\d+\.mp4$")
        round_files = sorted(
            [f for f in self.render_dir.glob("round-*-tick-*-to-*.mp4") if round_re.match(f.name)],
            key=lambda f: int(round_re.match(f.name).group(1)),
        )
        batch_files = sorted(
            [f for f in self.render_dir.glob("batch-*.mp4") if re.match(r"batch-\d+-\d+\.mp4$", f.name)],
            key=lambda f: int(re.match(r"batch-(\d+)-\d+\.mp4$", f.name).group(1)),
        )

        round_count = self.state["data"].get("round_count", 0)

        if round_files:
            nums = [int(round_re.match(f.name).group(1)) for f in round_files]
            missing = [n for n in range(1, (round_count or max(nums)) + 1) if n not in set(nums)]
            total_mb = sum(f.stat().st_size for f in round_files) / 1024 / 1024
            if missing:
                if skip_failed:
                    print(f"  [OK] {len(round_files)} of {round_count} round clip(s) "
                          f"({total_mb:.0f} MB) — {len(missing)} failed/skipped rounds: "
                          f"{missing[:20]}")
                else:
                    fail(2, "RENDER_INCOMPLETE",
                         f"missing round clips: {missing[:20]}{'...' if len(missing) > 20 else ''}")
            else:
                print(f"  [OK] {len(round_files)} round clip(s) ({total_mb:.0f} MB)")
        elif batch_files:
            # Legacy batch-*.mp4 layout
            if round_count > 0:
                last_batch = batch_files[-1]
                last_end = int(re.match(r"batch-\d+-(\d+)\.mp4$", last_batch.name).group(1))
                if last_end < round_count:
                    fail(2, "RENDER_INCOMPLETE",
                         f"last batch ends at round {last_end}, expected {round_count}")
            total_rounds = sum(
                int(re.match(r"batch-\d+-(\d+)\.mp4$", f.name).group(1))
                - int(re.match(r"batch-(\d+)-\d+\.mp4$", f.name).group(1))
                + 1
                for f in batch_files
            )
            total_mb = sum(f.stat().st_size for f in batch_files) / 1024 / 1024
            print(f"  [OK] {len(batch_files)} batch(es), {total_rounds} round(s) ({total_mb:.0f} MB)")
        else:
            fail(2, "RENDER_NO_CLIPS",
                 f"no round-*.mp4 or batch-*.mp4 files in {self.render_dir}")

    # ── Step 3: Concat ───────────────────────────────────────────────────

    def _copy_video_to_youtube(
        self, youtube_dir: Path, source: Path,
        label: str,
    ) -> None:
        """Copy ``source`` to ``youtube_dir/video.mp4`` unless a non-trivial
        video is already there. Preserves prior runs so re-invoking step 3
        (or resuming past it) doesn't clobber a working video.mp4 — e.g. one
        produced by an external overlay pass in a different render dir.
        ``label`` is just for log clarity ("raw" / "overlay")."""
        target = youtube_dir / "video.mp4"
        if target.exists() and target.stat().st_size > 1_000_000:
            print(f"  [skip] {label} video.mp4 already present "
                  f"({target.stat().st_size / 1e9:.1f} GB) >= 1MB; "
                  f"preserving (delete to force re-copy from {source.name})")
            return
        shutil.copy2(str(source), str(target))
        print(f"  [OK] Copied {label} video.mp4 "
              f"({target.stat().st_size / 1e9:.1f} GB)")

    def _find_round_offsets(self) -> Path | None:
        """Locate the round_offsets sidecar in the render dir. Returns None
        if not found. overlay_pov.py needs this for per-round tick mapping."""
        for candidate in (
            self.render_dir / f"{self.render_dir.name}.round_offsets.json",
            self.render_dir / "combined.round_offsets.json",
        ):
            if candidate.is_file():
                return candidate
        return None

    def _copy_round_offsets(self, youtube_dir: Path) -> None:
        if (youtube_dir / "video.round_offsets.json").exists():
            return
        src = self._find_round_offsets()
        if src is not None:
            shutil.copy2(str(src), str(youtube_dir / "video.round_offsets.json"))
            print(f"  [OK] Copied video.round_offsets.json to {youtube_dir.name}")

    def _load_analysis(self) -> dict | None:
        """Return the persisted csdm analysis json (rounds + tickrate).

        Re-exports from the demo via csdm json if the sidecar is missing
        (e.g. step 1 was skipped on resume and the file was cleaned).
        Returns None if the demo/analysis is unavailable."""
        saved = self.state["data"].get("analysis_json")
        if saved and Path(saved).is_file():
            try:
                return json.loads(Path(saved).read_text(encoding="utf-8"))
            except Exception:
                pass
        if not self.demo_path or not self.demo_path.exists():
            return None
        with tempfile.TemporaryDirectory() as tmp:
            r = subprocess.run(
                [CSDM, "json", str(self.demo_path), "--output-folder", tmp],
                capture_output=True, text=True, timeout=300,
            )
            if r.returncode != 0:
                return None
            jf = list(Path(tmp).glob("*.json"))
            if not jf:
                return None
            return json.loads(jf[0].read_text(encoding="utf-8"))

    def _pov_crosshair_code(self) -> str:
        """POV player's crosshair share code from the persisted csdm analysis.

        Reads only the step-1 sidecar (``render_dir/csdm_analysis.json``) —
        never re-runs csdm, so this stays cheap inside the title step. Returns
        "" when unavailable.
        """
        saved = (self.state.get("data") or {}).get("analysis_json")
        analysis_path = Path(saved) if saved else None
        if analysis_path is None or not analysis_path.is_file():
            cand = self.render_dir / "csdm_analysis.json"
            if cand.is_file():
                analysis_path = cand
        if analysis_path is None or not analysis_path.is_file():
            return ""
        try:
            data = json.loads(analysis_path.read_text(encoding="utf-8"))
        except Exception:
            return ""
        for pl in data.get("players", []):
            if str(pl.get("steamId") or "") == self.steam_id:
                return str(pl.get("crosshairShareCode") or "").strip()
        return ""

    def _faceit_kd(self) -> tuple[int, int] | None:
        """(kills, deaths) for the POV player, resolved from the backlog card
        or computed from the demo as a fallback.

        Caches the result into ``self.meta`` so both the title and thumbnail
        steps agree and we only parse the demo once per run.
        """
        if not self.is_faceit:
            return None
        kills = self.meta.get("kills")
        deaths = self.meta.get("deaths")
        if kills is not None and deaths is not None:
            return int(kills), int(deaths)
        kd = _faceit_kd_from_demo(self.demo_path, str(self.steam_id))
        if kd is not None:
            self.meta["kills"], self.meta["deaths"] = kd
        return kd

    @staticmethod
    def _probe_duration(path: Path) -> float:
        r = subprocess.run(
            [settings.ffprobe_exe, "-v", "quiet", "-print_format", "json",
             "-show_format", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            return 0.0
        data = json.loads(r.stdout)
        return float(data.get("format", {}).get("duration", 0) or 0)

    def _validate_concat(self, combined: Path, skip_failed: bool = False) -> None:
        """Cross-check the finished concatenated video against the csdm
        analysis. Two independent checks:

        1. ROUND COUNT (hard fail): concat's round count must equal the
           number of rounds in the demo analysis. Catches a dropped or
           duplicated round clip.

        2. DURATION (only when ground-truth per-round tick data exists):
           when the concat sidecar recorded actual rendered tick spans
           (CSDM `sequence-*-tick-*.mp4` files were present at concat time),
           sum those ACTUAL rendered spans / tickrate and compare to the
           probed video duration. This catches a round silently dropped or
           truncated during concat.

        NOTE: we do NOT compare against the full-round tick spans from the
        demo analysis. A player-POV `--event rounds` render is routinely
        shorter than the regulation-max round ticks (player death / shorter
        clips), so that comparison would false-fail on every run."""
        analysis = self._load_analysis()
        if not analysis:
            print("  [warn] no csdm analysis available; skipping concat validation")
            return
        rounds = analysis.get("rounds", [])
        tickrate = analysis.get("tickrate", 0) or self.state["data"].get("tickrate", 0)
        if not rounds or not tickrate:
            print("  [warn] analysis missing rounds/tickrate; skipping concat validation")
            return

        expected_rounds = len(rounds)

        # Combined round count (prefer the concat sidecar, else batch files).
        offsets = self._find_round_offsets()
        off = None
        if offsets and offsets.is_file():
            off = json.loads(offsets.read_text(encoding="utf-8"))
            actual_rounds = off.get("total_rounds", 0)
        else:
            batch_files = sorted(
                [f for f in self.render_dir.glob("batch-*.mp4")
                 if re.match(r"batch-\d+-\d+\.mp4$", f.name)],
                key=lambda f: int(re.match(r"batch-(\d+)-\d+\.mp4$", f.name).group(1)),
            )
            actual_rounds = sum(
                int(re.match(r"batch-\d+-(\d+)\.mp4$", f.name).group(1))
                - int(re.match(r"batch-(\d+)-\d+\.mp4$", f.name).group(1)) + 1
                for f in batch_files
            )
        # For split (p1/p2) demos the step-1 analysis only covers the part csdm
        # analyzed (e.g. p1), so actual concat rounds legitimately exceed the
        # analysis count. Only a *drop* (actual < expected) is an error; a
        # higher actual count is the normal split-demo case.
        if actual_rounds and actual_rounds < expected_rounds:
            if skip_failed:
                print(f"  [OK] {actual_rounds} of {expected_rounds} rounds "
                      f"concat'd ({expected_rounds - actual_rounds} skipped via --skip-failed-rounds)")
            else:
                fail(3, "CONCAT_ROUND_COUNT_MISMATCH",
                     f"concat has {actual_rounds} rounds but csdm analysis expects "
                     f"{expected_rounds} (rounds dropped during concat?)")
        else:
            print(f"  [OK] round count: {actual_rounds} rounds concat'd "
                  f"(analysis {expected_rounds})")

        # Sidecar self-consistency + match against the real combined.mp4
        # duration. Catches corrupt total_duration_seconds / round offsets
        # past EOF (e.g. probing cumulative combined after each batch append).
        if off is not None:
            from concat_rounds import validate_round_offsets_sidecar
            actual_sec = self._probe_duration(combined)
            sidecar_errs = validate_round_offsets_sidecar(
                off, video_duration_seconds=actual_sec if actual_sec > 0 else None,
                allow_gaps=skip_failed,
            )
            if sidecar_errs:
                if skip_failed:
                    for e in sidecar_errs:
                        print(f"  [warn] sidecar: {e}")
                else:
                    fail(3, "CONCAT_SIDECAR_INVALID", "; ".join(sidecar_errs))
            if not sidecar_errs:
                print(f"  [OK] sidecar validated against combined.mp4 "
                      f"({actual_sec:.1f}s)")

        # Duration check — only against ACTUAL rendered tick spans. These are
        #         # Duration estimate from CSDM's own analysis JSON (per-round
        # startTick/endTick spans). This is the actual tick-to-vid-second
        # mapping: sum of (endTick - startTick + 1) / tickrate for each
        # rendered round. The player-POV render window is routinely shorter
        # than the full round span (lead-in/trail, death cutoff), so the
        # estimate is only approximate -- use a loose tolerance and WARN
        # (don't hard-fail). The round-count check above is the authoritative
        # "match up" guarantee.
        rendered = set()
        for b in (off or {}).get("batches", []):
            for rn in range(int(b.get("round_start", 1)), int(b.get("round_end", 1)) + 1):
                rendered.add(rn)
        if not rendered:
            rendered = set(range(1, expected_rounds + 1))
        expected_sec = self._expected_duration_from_analysis(tickrate, sorted(rendered))
        if expected_sec is None:
            print("  [warn] no per-round tick data and no analysis spans;"
                  "skipping duration validation")
            return
        dur_src = "CSDM analysis round spans"
        # Loose: player-POV clip is typically shorter than the full round
        # span, so only catch GROSS truncation (a whole batch missing).
        tol = 0.35 * expected_sec + 2.0 * expected_rounds
        if expected_sec <= 0:
            return
        actual_sec = self._probe_duration(combined)
        if actual_sec <= 0:
            print("  [warn] could not probe combined.mp4 duration; skipping duration check")
            return
        # A missing/truncated round CLIPS the video (shorter) -- that's the
        # failure to catch. Extra padding (csdm lead-in per round) is benign.
        diff = actual_sec - expected_sec
        if diff < -tol:
            print(f"  [WARN] combined.mp4 {actual_sec:.1f}s is SHORTER than expected "
                  f"{expected_sec:.1f}s ({dur_src}; diff {diff:.1f}s < -tol "
                  f"{-tol:.1f}s). Estimate is approximate (player-POV window is "
                  f"shorter than full round span); round-count check passed, so "
                  f"continuing -- but verify no round was truncated.")
        elif diff > tol:
            print(f"  [warn] combined.mp4 {actual_sec:.1f}s LONGER than expected "
                  f"{expected_sec:.1f}s (diff +{diff:.1f}s > tol {tol:.1f}s); "
                  f"likely csdm per-round lead-in -- accepted")
        else:
            print(f"  [OK] duration: {actual_sec:.1f}s vs expected "
                  f"{expected_sec:.1f}s ({dur_src}; diff {diff:+.1f}s, tol +/-{tol:.1f}s")

    def _expected_duration_from_analysis(self, tickrate: float, round_nums: list[int]) -> float | None:
        """Fallback expected duration from CSDM's own analysis JSON.

        Uses the per-round startTick/endTick spans CSDM recorded when it
        analyzed the demo. Reliable ground truth with NO demoparser2
        overcount (the old estimate paired round n's start with round n+1's
        end and overcounted ~2.5x). The actual player-POV render window is
        routinely shorter than the full round span, so callers should use a
        LOOSE tolerance and treat shortfalls as warnings, not hard failures.
        Returns None if no analysis/rounds are available."""
        analysis = self._load_analysis()
        if not analysis:
            return None
        rounds = analysis.get("rounds", [])
        if not rounds or not tickrate:
            return None
        by_num = {int(r.get("number", 0)): r for r in rounds}
        total = 0.0
        used = 0
        for n in round_nums:
            r = by_num.get(n)
            if not r:
                continue
            s = r.get("startTick")
            e = r.get("endTick")
            if s is None or e is None or e <= s:
                continue
            total += (e - s + 1) / tickrate
            used += 1
        return total if used else None

    def _copy_overlay_result_to_youtube(self, overlay: Path) -> None:
        """Copy .overlay.mp4 from renders work dir to overlay youtube dir.

        Resume-safe only when the target is already as new as ``overlay`` and
        large enough. An older leftover youtube video must not block a freshly
        rendered overlay (that previously dropped jump/PiP fixes).
        """
        if self.skip_overlay:
            print("  [skip] raw-only: no overlay youtube dir")
            return
        target = self.youtube_dir / "video.mp4"
        if (
            target.exists()
            and target.stat().st_size > 1_000_000
            and overlay.exists()
            and target.stat().st_mtime >= overlay.stat().st_mtime
            and target.stat().st_size >= overlay.stat().st_size * 0.95
        ):
            print(f"  [skip] overlay video.mp4 already present "
                  f"({target.stat().st_size / 1e9:.1f} GB, up to date)")
            return
        if not overlay.exists():
            fail(4, "OVERLAY_OUTPUT_MISSING",
                 f"overlay result not found: {overlay}")
        self.youtube_dir.mkdir(parents=True, exist_ok=True)
        if target.exists():
            print(f"  [replace] outdated youtube overlay "
                  f"({target.stat().st_size / 1e9:.1f} GB) <- "
                  f"{overlay.name} ({overlay.stat().st_size / 1e9:.1f} GB)")
            target.unlink()
        shutil.copy2(str(overlay), str(target))
        print(f"  [OK] Copied overlay video.mp4 "
              f"({target.stat().st_size / 1e9:.1f} GB)")

    def step_concat(self) -> None:
        if not self.render_dir.exists():
            fail(3, "CONCAT_RENDER_DIR_MISSING", f"render dir not found: {self.render_dir}")
        skip_failed = getattr(self.args, "skip_failed_rounds", False)

        concat_args = ["scripts/pov/concat_rounds.py", str(self.render_dir)]
        export_w = int(getattr(self.args, "width", None) or 0)
        export_h = int(getattr(self.args, "height", None) or 0)
        if export_w >= 800 and export_h >= 600:
            concat_args += ["--width", str(export_w), "--height", str(export_h)]
        scaling = (self.meta.get("scaling_mode") or "").strip()
        if scaling:
            concat_args += ["--scaling-mode", scaling]
        if skip_failed:
            concat_args += ["--allow-gaps"]
        # Voice is ONE feature: the shade indicator AND the comms audio always
        # go together. --enable-voice-comms turns on both. (--voice-shade is a
        # legacy alias kept for backward-compat; it implies the same.)
        # The shade is applied at NATIVE res in the SCALE step so it stretches
        # together with the video (boxes stay locked to avatars).
        # FACEIT demos carry packet-aligned team voice; they enable voice by
        # default only when the demo has enough real team voice (see
        # _voice_enabled / VOICE_AUTO_MIN_TEAM_SECONDS).
        enable_voice = self._voice_enabled()
        if enable_voice and self.demo_path and self.demo_path.exists():
            cap_w = int(self.meta.get("capture_width") or 0)
            cap_h = int(self.meta.get("capture_height") or 0)
            concat_args += [
                "--voice-shade-demo", str(self.demo_path),
                "--voice-shade-steam-id", self.steam_id,
                "--voice-shade-fade", str(getattr(self.args, "voice_shade_fade", 0.3)),
                # first-half scoreboard side is auto-detected from the demo
                # (CT=LEFT, T=RIGHT) inside build_voice_shade_data
            ]
        r = self._run_py(concat_args, timeout=36000)
        if r.returncode != 0:
            fail(3, "CONCAT_FAILED", f"concat_rounds.py exited {r.returncode}")

        combined = self.render_dir / "combined.mp4"
        if not combined.exists():
            fail(3, "CONCAT_NO_COMBINED", f"no combined.mp4 found in {self.render_dir}")
        if combined.stat().st_size < 100000:
            fail(3, "CONCAT_OUTPUT_TOO_SMALL", f"combined.mp4 suspiciously small: {combined.stat().st_size} bytes")

        self._validate_concat(combined, skip_failed=skip_failed)

        if self.skip_overlay:
            self.youtube_dir.mkdir(parents=True, exist_ok=True)
            self._copy_video_to_youtube(self.youtube_dir, combined, label="raw")
            self._copy_round_offsets(self.youtube_dir)

    # ── Step 4: Overlay ───────────────────────────────────────────────────

    def step_overlay(self) -> None:
        """Apply keyboard + util flight overlay. Skipped in --raw-only mode."""
        # Skip in raw-only mode
        if self.skip_overlay:
            print("  [skip] Raw-only mode: overlay step disabled")
            return

        # Voice is ONE feature: the comms AUDIO mix runs here in step 4
        # (the shade indicator was applied at native res in the step-3 scale).
        # --enable-voice-comms turns both on; --voice-shade is a legacy alias.
        # Keep FACEIT voice shade and team comms inseparable. FACEIT cards
        # enable this by default only when the demo has enough team voice.
        enable_voice = self._voice_enabled()

        # Skip if the overlay variant already has a valid video (resume from
        # a previous successful run where .overlay_work was cleaned).
        if not self.skip_overlay:
            dst = self.youtube_dir / "video.mp4"
            if dst.is_file() and dst.stat().st_size > 100_000:
                # Re-overlay if the source combined.mp4 was re-baked (e.g. a
                # step-3 shade re-run) after this overlay was last produced;
                # otherwise the stale shade/voice would be shipped as-is.
                src = self.render_dir / "combined.mp4"
                re_baked = src.is_file() and src.stat().st_mtime > dst.stat().st_mtime
                if not re_baked:
                    print(f"  [skip] Overlay video already exists in "
                          f"{self.youtube_dir.name}/video.mp4")
                    return
                print(f"  [re-overlay] combined.mp4 is newer than the existing "
                      f"overlay; re-running overlay_pov.py")

        # Work under renders/ so intermediate files (batches, sidecar)
        # don't clutter youtube/. Only copy final result to youtube/.
        target_dir = self.render_dir / ".overlay_work"

        video_path = target_dir / "video.mp4"
        # Ensure the round_offsets sidecar is always present next to video.mp4.
        # overlay_pov.py needs <video>.round_offsets.json for tick->frame sync.
        # The copy below is normally done together with video.mp4, but if a
        # previous run left video.mp4 behind without its sidecar (e.g. killed
        # between the two copies), re-copy the sidecar so resume works.
        sidecar_dst = target_dir / "video.round_offsets.json"
        sidecar_src = self.render_dir / "combined.round_offsets.json"
        if video_path.exists() and not sidecar_dst.exists() and sidecar_src.is_file():
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(sidecar_src), str(sidecar_dst))
            print(f"  [setup] re-copied missing sidecar to {sidecar_dst.name}")
        if not video_path.exists():
            # Auto-recover from render-side overlay artifacts only.
            # Do NOT treat youtube/..._overlay/video.mp4 as a resume source —
            # that is the destination we may be intentionally regenerating.
            external = self._find_overlay_video(include_youtube=False)
            if external is not None and external.exists():
                target_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(external), str(video_path))
                print(f"  [recover] copied pre-rendered overlay "
                      f"({external.stat().st_size / 1e9:.1f} GB) from "
                  f"{external.parent.name}/{external.name} -> overlay work dir; "
                      f"skipping overlay_pov.py (already overlaid)")
                self._copy_overlay_result_to_youtube(overlay=video_path)
                return
            else:
                # Copy raw combined.mp4 to renders/.overlay_work/ as working file.
                src = self.render_dir / "combined.mp4"
                if not src.exists() or src.stat().st_size < 100_000:
                    fail(4, "OVERLAY_NO_INPUT",
                         f"combined.mp4 missing/empty in render_dir")
                target_dir.mkdir(parents=True, exist_ok=True)
                # Hardlink instead of copy: combined.mp4 can be 30+ GB after
                # the 1440p scale, and a full copy doubles disk usage (and
                # fails outright when the disk is nearly full). Same volume
                # => hardlink costs nothing; overlay_pov.py only READS the
                # input and writes video.overlay.mp4 separately.
                try:
                    os.link(str(src), str(video_path))
                    linked = True
                except OSError:
                    # Cross-volume or filesystem without hardlinks -> copy.
                    shutil.copy2(str(src), str(video_path))
                    linked = False
                # Copy sidecar (round_offsets.json) alongside video.mp4
                # overlay_pov.py looks for <video>.round_offsets.json next to video
                sidecar_src = self.render_dir / "combined.round_offsets.json"
                if sidecar_src.is_file():
                    sidecar_dst = target_dir / "video.round_offsets.json"
                    shutil.copy2(str(sidecar_src), str(sidecar_dst))
                    print(f"  [setup] copied sidecar to {sidecar_dst.name}")
                verb = "hardlinked" if linked else "copied"
                print(f"  [setup] {verb} combined.mp4 into renders/.overlay_work/ "
                      f"for overlay_pov.py")
        if not self.demo_path or not self.demo_path.exists():
            fail(4, "OVERLAY_NO_DEMO",
                 f"demo required for overlay but not found: {self.demo_path}")
            return
        steam_id = self.state["data"].get("steam_id", self.meta.get("steam_id", ""))
        if not steam_id:
            fail(4, "OVERLAY_NO_STEAM_ID", "no steam_id for overlay")
            return

        work_dir = self.render_dir / ".overlay_work"
        work_dir.mkdir(parents=True, exist_ok=True)
        from overlay.overlay_pov import run_overlay
        from overlay.overlay_utilcams import _ensure_cs2util_data
        try:
            _ensure_cs2util_data(Path(self.demo_path))
            run_overlay(
                Path(video_path),
                Path(self.demo_path),
                steam_id,
                None,
                getattr(self.args, "overlay_batches", 10),
                util_cams_root=self.render_dir / "utility_cams",
                work_dir=work_dir,
            )
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
            if code:
                fail(4, "OVERLAY_FAILED", f"overlay_pov exited {code}")

        overlay_sidecar = video_path.with_suffix(".overlay.mp4")
        if not overlay_sidecar.exists():
            fail(4, "OVERLAY_NO_OUTPUT",
                 f"overlay_pov.py succeeded but {overlay_sidecar} not found")
            return

        # -- Voice comms: mix POV-team voice into the overlay audio. ---------
        # Runs only when voice is enabled, and always together with the shade
        # (the shade is baked into overlay_sidecar by overlay_pov above). Uses
        # the FIXED packet-aligned decoder; video is stream-copied (no re-encode).
        if enable_voice:
            offsets_for_comms = target_dir / "video.round_offsets.json"
            if not offsets_for_comms.is_file():
                offsets_for_comms = self.render_dir / "combined.round_offsets.json"
            comms_out = video_path.with_name("video.overlay.voice.mp4")
            r = self._run_py([
                "scripts/faceit/mix_team_voice.py",
                "--demo", str(self.demo_path),
                "--video", str(overlay_sidecar),
                "--steam-id", steam_id,
                "--offsets", str(offsets_for_comms),
                "--out", str(comms_out),
                "--force",
            ], timeout=7200, capture_output=True, text=True)
            if r.stdout:
                print(r.stdout)
            if r.returncode != 0:
                if r.stderr:
                    print("[stderr]", r.stderr[-2000:])
                fail(4, "VOICE_COMMS_FAILED", f"mix_team_voice.py exited {r.returncode}")
            if comms_out.exists() and comms_out.stat().st_size > 100_000:
                comms_out.replace(overlay_sidecar)
                print(f"  [voice-comms] mixed team voice into {overlay_sidecar.name}")

        # Copy overlay result from renders/ work dir -> youtube/ variant dir
        self._copy_overlay_result_to_youtube(overlay=overlay_sidecar)
        # Free the big mezzanine files immediately: per-round clips + native
        # pre-scale source are dead weight once the overlay exists.
        self._purge_render_intermediates(full=False)

    def _append_outro(self, youtube_dir: Path, step_num: int = 5) -> None:
        """Generate a 5s silent outro and append it to video.mp4 inside
        ``youtube_dir``. Used for both raw and overlay variants."""
        video = youtube_dir / "video.mp4"
        if not video.exists():
            fail(step_num, "OUTRO_VIDEO_MISSING", f"video.mp4 not found in {youtube_dir}")

        self._run_py(["scripts/pov/generate_outro.py", str(video)], timeout=120)

        outro = youtube_dir / "outro.mp4"
        if not outro.exists():
            fail(step_num, "OUTRO_CLIP_MISSING", f"outro.mp4 not generated in {youtube_dir}")
        if outro.stat().st_size < 1000:
            fail(step_num, "OUTRO_TOO_SMALL", f"outro.mp4 too small: {outro.stat().st_size} bytes")

        temp = youtube_dir / "video.temp.mp4"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(f"file '{video.resolve()}'\n")
            f.write(f"file '{outro.resolve()}'\n")
            list_path = f.name

        r = subprocess.run(
            [settings.ffmpeg_exe, "-f", "concat", "-safe", "0", "-i", list_path,
             "-c", "copy", str(temp)],
            capture_output=True, text=True, timeout=3600,
        )
        Path(list_path).unlink(missing_ok=True)

        if r.returncode != 0:
            temp.unlink(missing_ok=True)
            fail(step_num, "OUTRO_CONCAT_FAILED", f"ffmpeg concat failed: {r.stderr[-300:]}")

        temp.replace(video)
        outro.unlink()
        vid_mb = video.stat().st_size / 1024 / 1024
        print(f"  [OK] Outro appended in {youtube_dir.name}/ ({vid_mb:.0f} MB)")

    def _purge_render_intermediates(self, full: bool = False) -> None:
        """Reclaim disk space by deleting render intermediates.

        Disk exhaustion ([WinError 112]) during overlay was caused by keeping
        ~50 GB of mezzanine files alive long after they were consumed:
          - round-*.mp4 / batch-*.mp4   (per-round CSDM clips, post-concat)
          - combined.native.mp4         (pre-scale source)
          - .overlay_work/              (hardlinked/copied working video)
          - combined.mp4                (scaled 1440p mezzanine)

        light (full=False, end of step 4): delete everything EXCEPT
            combined.mp4 + sidecar — those still allow a re-overlay without
            a full re-render if a later step fails.
        full=True (after step 6 -> video is youtube-ready): delete the whole
            renders/pov-* folder. The finished video lives in youtube/, and
            upload_pending.py only reads youtube/*/upload_meta.json.

        Opt out with --keep-intermediates.
        """
        if getattr(self.args, "keep_intermediates", False):
            return
        if not self.render_dir.exists():
            return
        freed = 0
        try:
            if full:
                freed = sum(f.stat().st_size for f in self.render_dir.rglob("*") if f.is_file())
                shutil.rmtree(self.render_dir, ignore_errors=False)
                print(f"  [cleanup] removed {self.render_dir.name}/ "
                      f"({freed / 1e9:.1f} GB) — video is youtube-ready")
                return
            patterns = ("round-*.mp4", "batch-*.mp4")
            targets: list[Path] = []
            for pat in patterns:
                targets.extend(self.render_dir.glob(pat))
            native = self.render_dir / "combined.native.mp4"
            if native.exists():
                targets.append(native)
            work = self.render_dir / ".overlay_work"
            for t in targets:
                try:
                    freed += t.stat().st_size
                    t.unlink()
                except OSError:
                    pass
            if work.exists():
                shutil.rmtree(work, ignore_errors=True)
            if freed > 0:
                print(f"  [cleanup] freed {freed / 1e9:.1f} GB of render "
                      f"intermediates (kept combined.mp4 for re-overlay)")
        except OSError as e:
            print(f"  [WARN] intermediate cleanup failed (non-fatal): {e}")

    def step_outro(self) -> None:
        self._append_outro(self.youtube_dir, step_num=5)

    # ── Step 6: Thumbnail ────────────────────────────────────────────────

    def _find_overlay_video(self, *, include_youtube: bool = True) -> Path | None:
        """Locate a pre-rendered overlay video.

        Search order:
          1. ``youtube/{run_id}_overlay/video.mp4`` (finished variant; for
             thumbnail bg only — NOT for step-4 resume, or a re-run would
             circularly "recover" the bad/stale youtube file and skip
             overlay_pov.py)
          2. ``renders/.../.overlay_work/video.overlay.mp4``
          3. ``renders/.../combined.overlay.mp4`` (+ legacy render-dir names)
        """
        candidates: list[Path] = []
        if include_youtube and not self.skip_overlay:
            candidates.append(self.youtube_dir / "video.mp4")
        if self.render_dir:
            candidates.append(self.render_dir / ".overlay_work" / "video.overlay.mp4")
            candidates.append(self.render_dir / "combined.overlay.mp4")
        if self.demo_path and self.steam_id:
            dem_stem = self.demo_path.stem
            player_slug = run_id_from_name(self.player)
            candidates.extend([
                PROJECT_ROOT / "renders" / f"pov-{dem_stem}_{self.steam_id}_full" / "combined.overlay.mp4",
                PROJECT_ROOT / "renders" / f"pov-{dem_stem}_{self.player}_full" / "combined.overlay.mp4",
                PROJECT_ROOT / "renders" / f"pov-{dem_stem}_{player_slug}" / "combined.overlay.mp4",
            ])
        for c in candidates:
            if c.is_file() and c.stat().st_size > 1024 * 1024:
                return c
        return None

    def _extract_overlay_frame(self, overlay_video: Path) -> Path | None:
        """Extract a single frame from the overlay video at ~40% of duration.

        Returns a temp JPEG path. Returns None on ffmpeg failure.
        """
        try:
            probe = subprocess.run(
                [settings.ffprobe_exe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(overlay_video)],
                capture_output=True, text=True, timeout=60,
            )
            if probe.returncode != 0:
                return None
            duration = float(probe.stdout.strip())
            seek_t = max(0.5, duration * 0.40)
            fd, name = tempfile.mkstemp(prefix="thumb_overlay_", suffix=".jpg")
            os.close(fd)
            tmp = Path(name)
            r = subprocess.run(
                [settings.ffmpeg_exe, "-y", "-loglevel", "error",
                 "-ss", f"{seek_t:.3f}", "-i", str(overlay_video),
                 "-frames:v", "1",
                 "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,"
                        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2",
                 str(tmp)],
                capture_output=True, text=True, timeout=120,
            )
            if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size < 1024:
                tmp.unlink(missing_ok=True)
                return None
            return tmp
        except Exception as e:
            print(f"  [warn] overlay frame extraction failed: {e}")
            return None

    def _extract_kill_frame(self, video_path: Path) -> Path | None:
        """Frame from the finished POV/overlay video at the densest killfeed
        tick. Tick → seconds via the concat sidecar. No CS2.
        """
        from thumbnail.utils import extract_killfeed_frame

        extra: list[Path] = []
        analysis: Path | None = None
        if self.render_dir:
            extra.extend([
                self.render_dir / "combined.round_offsets.json",
                self.render_dir / f"{self.render_dir.name}.round_offsets.json",
            ])
            analysis_p = self.render_dir / "csdm_analysis.json"
            if analysis_p.is_file():
                analysis = analysis_p
        try:
            return extract_killfeed_frame(
                video_path, self.steam_id,
                demo_path=self.demo_path,
                extra_sidecars=extra,
                analysis_path=analysis,
            )
        except Exception as e:
            print(f"  [warn] kill-frame extraction failed: {e}")
            return None

    def _generate_thumbnail(self, youtube_dir: Path, variant: str, step_num: int = 6) -> None:
        """Generate a 1280x720 thumbnail in ``youtube_dir`` and write the
        corresponding upload_meta.json. ``variant`` is 'raw' (default) or
        'overlay' (adds W/ INPUT OVERLAY and + UTIL CAMS badges in bottom-right).

        For ``variant='overlay'``, the background is a frame from the finished
        overlay ``video.mp4`` (keyboard + util-cam already baked in) at the
        densest POV killfeed tick, mapped via the concat sidecar. No CS2.
        """
        _minimize_cs2()

        youtube_dir.mkdir(parents=True, exist_ok=True)
        thumb = youtube_dir / "thumbnail.jpg"

        if self.is_faceit:
            # FACEIT path: style-01 (proof_01 HTML) via faceit_thumbnail.py.
            # ELO / K-D come from the backlog card; portraits from demos/avatars.
            # Background: kill-moment frame from the finished youtube video
            # (overlay-only = keyboard + util cam, player's render cfg).
            faceit_bg: Path | None = None
            pov_vid = youtube_dir / "video.mp4"
            if pov_vid.is_file():
                faceit_bg = self._extract_kill_frame(pov_vid)
            cmd = [
                "scripts/faceit/faceit_thumbnail.py", str(self.demo_path),
                "--player", self.player,
                "--map", self.map_name,
                "--variant", variant,
            ]
            if faceit_bg is not None:
                cmd += ["--background", str(faceit_bg)]
            elif pov_vid.is_file():
                cmd += ["--video", str(pov_vid)]
            if self.steam_id:
                cmd += ["--steam-id", self.steam_id]
            elo = self.meta.get("elo")
            opp_elo = self.meta.get("opp_avg_elo")
            if elo is not None and opp_elo is not None:
                cmd += ["--elo", str(elo), "--opp-elo", str(opp_elo)]
            kd = self._faceit_kd()
            if kd is not None:
                kills, deaths = kd
                cmd += ["--kd", f"{kills}/{deaths}"]
            cmd += ["--output", str(youtube_dir)]
            r = self._run_py(cmd, timeout=900)
            if faceit_bg is not None:
                faceit_bg.unlink(missing_ok=True)
            if r.returncode != 0:
                fail(step_num, "THUMBNAIL_FAILED",
                     f"faceit thumbnail exited {r.returncode} for variant={variant}")
            if not thumb.exists():
                fail(step_num, "THUMBNAIL_MISSING", f"thumbnail not created at {thumb}")
            if thumb.stat().st_size < 1000:
                fail(step_num, "THUMBNAIL_TOO_SMALL", f"thumbnail too small: {thumb.stat().st_size}")
            print(f"  [OK] FACEIT Thumbnail [{variant}]: {thumb.name}")
            self._write_upload_meta(youtube_dir, variant=variant, step_num=step_num)
            return

        bg_override: Path | None = None
        bg_cleanup: Path | None = None
        pov_vid = youtube_dir / "video.mp4"
        if not pov_vid.is_file() and variant == "overlay":
            found = self._find_overlay_video()
            if found is not None:
                pov_vid = found
        if pov_vid.is_file():
            bg_override = self._extract_kill_frame(pov_vid)
            if bg_override is not None:
                print(f"  [bg] kill frame from {pov_vid.name}")
            elif variant == "overlay":
                print(f"  [bg] no sidecar/kill match — mid-video frame")
                bg_override = self._extract_overlay_frame(pov_vid)
            bg_cleanup = bg_override
        else:
            print(f"  [bg] finished video not found — thumbnail needs --background")

        cmd = [
            "-m", "thumbnail",
            self.hltv_url,
            "--player", self.player,
            "--map", self.map_name,
            "--variant", variant,
        ]
        if bg_override is not None:
            cmd += ["--background", str(bg_override)]
        if self.steam_id:
            cmd += ["--steam-id", self.steam_id]
        if self.tournament:
            cmd += ["--tournament", self.tournament]
        ewc_logo = _tournament_logo_path(self.tournament, self.hf_root)
        if ewc_logo:
            cmd += ["--tournament-logo", str(ewc_logo)]
        cmd += ["--output", str(youtube_dir)]

        r = self._run_py(cmd, timeout=900)
        if r.returncode != 0:
            if bg_cleanup:
                bg_cleanup.unlink(missing_ok=True)
            fail(step_num, "THUMBNAIL_FAILED",
                 f"thumbnail generator exited {r.returncode} for variant={variant}")

        if not thumb.exists():
            if bg_cleanup:
                bg_cleanup.unlink(missing_ok=True)
            fail(step_num, "THUMBNAIL_MISSING", f"thumbnail not created at {thumb}")
        if thumb.stat().st_size < 1000:
            if bg_cleanup:
                bg_cleanup.unlink(missing_ok=True)
            fail(step_num, "THUMBNAIL_TOO_SMALL",
                 f"thumbnail too small: {thumb.stat().st_size} bytes")

        try:
            from PIL import Image
            im = Image.open(thumb)
            if im.size != (1280, 720):
                if bg_cleanup:
                    bg_cleanup.unlink(missing_ok=True)
                fail(step_num, "THUMBNAIL_BAD_SIZE",
                     f"thumbnail dimensions {im.size} != expected 1280x720")
        except ImportError:
            fail(step_num, "THUMBNAIL_PIL_MISSING",
                 "PIL/Pillow not installed — required for thumbnail dimension validation")

        if bg_cleanup:
            try:
                bg_cleanup.unlink(missing_ok=True)
            except OSError:
                pass  # Windows file lock — temp file is small, harmless to leave

        print(f"  [OK] Thumbnail [{variant}]: {thumb.name}")
        self._write_upload_meta(youtube_dir, variant=variant, step_num=step_num)

    def step_thumbnail(self) -> None:
        variant = "raw" if self.skip_overlay else "overlay"
        self._generate_thumbnail(self.youtube_dir, variant=variant, step_num=6)

    # ── Upload meta writer (runs inside thumbnail step) ────────────────

    def _write_upload_meta(
        self,
        youtube_dir: Path,
        variant: str = "raw",
        step_num: int = 5,
    ) -> None:
        """Generate title/desc/tags via generate_title.py and write the
        resulting upload_meta.json into ``youtube_dir``. Pass
        ``variant='overlay'`` to suffix title/desc/tags for the overlay product."""
        video = youtube_dir / "video.mp4"
        thumb = youtube_dir / "thumbnail.jpg"

        titlize_args = [
            "scripts/pov/generate_title.py", str(self.ratings_json),
            "--player", self.player,
            "--map", self.map_name,
            "--variant", variant,
        ]
        if self.is_faceit:
            titlize_args = [
                "scripts/faceit/faceit_title.py", str(self.demo_path),
                "--player", self.player,
                "--map", self.map_name,
            ]
            if self.steam_id:
                titlize_args += ["--steam-id", self.steam_id]
            elo = self.meta.get("elo")
            opp_elo = self.meta.get("opp_avg_elo")
            if elo is not None and opp_elo is not None:
                titlize_args += ["--elo", str(elo), "--opp-elo", str(opp_elo)]
            mid = self.meta.get("faceit_match_id")
            if mid:
                titlize_args += ["--match-id", str(mid)]
            kd = self._faceit_kd()
            if kd is not None:
                kills, deaths = kd
                titlize_args += ["--kd", f"{kills}/{deaths}"]
            # FACEIT POVs mix in team voice comms when voice is enabled
            # (auto-detected by team-voice volume, or explicit flag).
            if self._voice_enabled():
                titlize_args += ["--voice-comms"]

        # Crosshair + viewmodel + video settings are added to BOTH the HLTV and
        # FACEIT title paths (prosettings-driven "Settings (as rendered)").
        code = self._pov_crosshair_code()
        if code:
            titlize_args += ["--crosshair-code", code]
        for flag, key in (
            ("--viewmodel-fov", "viewmodel_fov"),
            ("--viewmodel-offset-x", "viewmodel_offset_x"),
            ("--viewmodel-offset-y", "viewmodel_offset_y"),
            ("--viewmodel-offset-z", "viewmodel_offset_z"),
            ("--viewmodel-presetpos", "viewmodel_presetpos"),
            ("--resolution", "resolution"),
            ("--aspect-ratio", "aspect_ratio"),
            ("--scaling-mode", "scaling_mode"),
            ("--video-settings-source", "video_settings_source"),
        ):
            val = self.meta.get(key)
            if val is not None and val != "":
                titlize_args += [flag, str(val)]
        if self.tournament and not self.is_faceit:
            titlize_args += ["--tournament", self.tournament]

        r = self._run_py(titlize_args, capture_output=True, text=True, timeout=15)

        meta = {}
        if r.returncode == 0 and r.stdout.strip():
            try:
                meta = json.loads(r.stdout.strip())
            except json.JSONDecodeError:
                pass

        upload_meta = {
            "title": meta.get("title") or f"{self.player} | {self.map_name}",
            "description": meta.get("description", ""),
            "tags": meta.get("tags", []),
            "video_path": str(video),
            "thumbnail_path": str(thumb) if thumb.exists() else None,
            "privacy": "private",
            "playlist_name": normalize_playlist_name(self.tournament),
            "youtube_id": None,
            "upload_status": "pending",
            "variant": variant,
        }

        meta_path = youtube_dir / "upload_meta.json"
        meta_path.write_text(json.dumps(upload_meta, indent=2))
        print(f"  [OK] upload_meta.json written [{variant}]")

    # ── Step 7: Cleanup ────────────────────────────────────────────────

    def step_cleanup(self) -> None:
        removed = []
        if self.render_dir.exists():
            shutil.rmtree(self.render_dir)
            removed.append(str(self.render_dir))
            print(f"  Removed: {self.render_dir.name}")

        state_path = STATE_DIR / f"{self.run_id}.json"
        if state_path.exists():
            state_path.unlink()
            removed.append(f".pipeline/{self.run_id}.json")

        if self.render_dir.exists():
            fail(8, "CLEANUP_RENDER_DIR_FAILED", f"render dir still exists after rmtree: {self.render_dir}")
        if state_path.exists():
            fail(8, "CLEANUP_STATE_FAILED", f"pipeline state file still exists: {state_path}")
        if not removed:
            print("  Nothing to clean up")
        else:
            print(f"  [OK] Removed {len(removed)} item(s)")


def main() -> None:
    parser = argparse.ArgumentParser(description="E2E Pipeline: backlog .md -> render -> thumbnail -> upload")
    parser.add_argument("--backlog", required=True, help="Path to backlog markdown file with BACKLOG_META")
    parser.add_argument("--step", type=int, default=None, choices=range(1, 8),
                        help="Start from step N (1=analyze..7=cleanup). "
                             "Default: auto-resume from last completed step.")
    parser.add_argument("--until", type=int, default=None, choices=range(1, 8),
                        help="Stop after step N (default: 6 = thumbnail, before cleanup)")
    parser.add_argument(
        "--raw-only",
        action="store_true",
        dest="raw_only",
        help="Produce only the raw variant (no overlay).",
    )
    parser.add_argument(
        "--overlay-only",
        action="store_true",
        help="Deprecated no-op: overlay-only is the default.",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=None,
        help="HLAE + concat export width (default: player capture width, concat 2560).",
    )
    parser.add_argument(
        "--height",
        type=int,
        default=None,
        help="HLAE + concat export height (default: player capture height, concat 1440).",
    )
    parser.add_argument(
        "--batches",
        type=int,
        default=1,
        help="Number of render batches (default: 1). Rounds are divided equally across batches "
             "and rendered in N separate CSDM calls. Each call launches a fresh CS2 (HLAE hook), "
             "so keep 1 to minimize the flaky vanilla-viewer hook failure.",
    )
    parser.add_argument(
        "--hook-timeout",
        type=float,
        default=150.0,
        help="Seconds to wait for a new >=1 MB sequence file before declaring a failed HLAE "
             "hook (default: 150). The vanilla-viewer failure produces nothing.",
    )
    parser.add_argument(
        "--hook-retries",
        type=int,
        default=2,
        help="Times to kill + relaunch a batch when the HLAE hook fails to engage "
             "(default: 2). 0 disables hook detection.",
    )
    parser.add_argument(
        "--overlay-batches",
        type=int,
        default=10,
        help="Rounds per overlay batch (default: 10). Splits overlay ffmpeg "
             "composite into per-batch segments for ~2-3x speedup and crash "
             "resume. Set 0 for single-pass (original behavior).",
    )
    parser.add_argument(
        "--enable-voice-comms",
        action="store_true",
        default=False,
        help="Enable BOTH the voice-activity shade indicator AND the POV-team "
             "voice comms (they always go together). Folds the shade into the "
             "batched overlay encode and mixes the team voice into the audio "
             "(via mix_team_voice.py).",
    )
    parser.add_argument(
        "--voice-shade",
        action="store_true",
        default=False,
        help="[legacy alias of --enable-voice-comms] Overlay a voice-activity "
             "shade + mix team voice comms.",
    )
    parser.add_argument(
        "--voice-shade-fade",
        type=float,
        default=0.3,
        help="Voice-shade fade duration in seconds (default: 0.3).",
    )
    parser.add_argument(
        "--skip-failed-rounds",
        action="store_true",
        default=False,
        help="[DANGER] Skip round batches that fail during rendering instead of aborting. "
             "NEVER set by default. Only use when a specific demo file is corrupted/incompatible "
             "and CS2 crashes on certain rounds. Silently drops failed batches, producing "
             "incomplete POV videos. Set per-invocation for problematic demos only. "
             "See AGENTS.md for details.",
    )
    parser.add_argument(
        "--keep-intermediates",
        dest="keep_intermediates",
        action="store_true",
        default=False,
        help="Keep renders/ intermediates after the video is youtube-ready "
             "(default: per-round clips + native source are deleted after "
             "overlay; the whole renders/pov-* folder is deleted once the "
             "pipeline completes step 6).",
    )
    parser.add_argument(
        "--cleanup",
        dest="no_cleanup",
        action="store_false",
        default=True,
        help="Run step 7 cleanup (delete renders/ + state file). "
             "Default: cleanup OFF (renders/ + pipeline state kept for "
             "re-runs). Pass --cleanup to opt in.",
    )
    args = parser.parse_args()

    meta = _parse_backlog(args.backlog)
    print(f"{'='*60}")
    print(f"  CS2Archive Pipeline")
    print(f"  Player: {meta.get('player', '?')} | Map: {meta.get('map', '?')}")
    print(f"  Demo:   {meta.get('demo_path', '(unknown)')}")
    print(f"  Step:   {args.step}" + (f" -> {args.until}" if args.until else f" -> 6 (thumbnail; upload handled by upload_pending.py)"))
    print(f"{'='*60}")

    Pipeline(args).run()


if __name__ == "__main__":
    main()
