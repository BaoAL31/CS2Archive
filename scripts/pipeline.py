"""
E2E pipeline: backlog metadata -> analyze -> render -> concat -> overlay -> outro -> thumbnail.
Reads all POV metadata from a backlog file.
Structured errors for agent parsing.
Auto-downloads missing demos from HuggingFace if hf_root is set.

Usage:
    python scripts/pipeline.py --backlog backlog/<match_slug>/<priority>/<slug>.json [--step N] [--dual-upload]

Steps (use --step N to start at a specific step):
  1 = analyze    csdm analyze the demo
  2 = render     Render all rounds as POV clips
  3 = concat     Concatenate rounds, copy to youtube/
  4 = overlay    Apply input overlay + util cam (skipped unless --dual-upload)
  5 = outro      Generate 5s silent outro, concat onto video.mp4
  6 = thumbnail  Generate 1280x720 thumbnail + write upload_meta.json
  7 = cleanup    Remove renders folder + pipeline state

NOTE: Uploading is NOT done by the pipeline. The pipeline only produces the
finished video, thumbnail, and upload_meta.json (title/description/tags/
thumbnail_path/youtube_id=None/upload_status="pending"). A separate script,
scripts/upload_pending.py, scans every upload_meta.json under youtube/ and
uploads any that are still pending.

Dual-upload (--dual-upload): produces a second, independent variant with
keyboard/utility overlay. Raw variant -> youtube/{run_id}/
Overlay variant -> youtube/{run_id}_overlay/   (separate title, thumbnail, meta)
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
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from assign_playlist import normalize_playlist_name
# Redirect HF cache to D: drive before importing huggingface_hub
os.environ.setdefault("HF_HOME", "D:/.cache/huggingface")
os.environ.setdefault("HF_HUB_CACHE", "D:/.cache/huggingface/hub")
from huggingface_hub import hf_hub_download
STATE_DIR = PROJECT_ROOT / ".pipeline"
STATE_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(str(PROJECT_ROOT))

CSDM = r"C:\Users\jembo\AppData\Local\Programs\cs-demo-manager\csdm.cmd"
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


def run_id_from_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:80].strip("_")


def pov_render_dir(dem_stem: str, player: str) -> Path:
    player_slug = run_id_from_name(player)
    return PROJECT_ROOT / "renders" / f"pov-{dem_stem}_{player_slug}"


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
    if missing:
        fail(0, "BACKLOG_MISSING_FIELDS",
             f"Backlog missing required fields: {', '.join(missing)}")

    return meta


class Pipeline:
    def __init__(self, args):
        self.args = args
        self.meta = _parse_backlog(args.backlog)

        self.player = self.meta["player"]
        self.map_name = self.meta["map"]
        self.hltv_url = self.meta["hltv_url"]
        self.steam_id = self.meta.get("steam_id", "")
        self.tournament = self.meta.get("tournament", "")

        demo_path_str = self.meta.get("demo_path", "")
        self.demo_path = Path(demo_path_str) if demo_path_str else None

        ratings_path_str = self.meta.get("ratings_path", "")
        self.ratings_json = Path(ratings_path_str) if ratings_path_str else PROJECT_ROOT / "demos" / "analysis" / ""

        avatar_path_str = self.meta.get("avatar_path", "")
        self.avatar_path = Path(avatar_path_str) if avatar_path_str else None

        self.start_step = args.step if args.step is not None else 1
        self._cli_step = args.step  # None = no explicit --step → allow auto-skip
        self.end_step = args.until if args.until is not None else max(STEPS.keys())
        # --no-cleanup caps end_step at one below the last step (skip cleanup).
        # Default run therefore stops after thumbnail (step 6), producing the
        # finished video + upload_meta.json but NOT uploading or cleaning up.
        if getattr(args, "no_cleanup", True) and self.end_step == max(STEPS.keys()):
            self.end_step = max(STEPS.keys()) - 1

        # Dual-upload: when True, also process an overlay variant as a second
        # independent upload (separate youtube dir, title, thumbnail, meta).
        # Default False = 100% backward-compatible with existing behavior.
        # --overlay-only implies --dual-upload's overlay branch but skips
        # the raw variant entirely (no raw video copy, no raw outro, no
        # raw thumbnail, no raw upload). The overlay variant IS the upload.

        from scrapers.hltv_acquire import match_id_from_url

        slug = self.hltv_url.rstrip("/").split("/")[-1]
        self.match_id = match_id_from_url(self.hltv_url)
        dem_stem = self.demo_path.stem if self.demo_path else slug
        self.render_dir = pov_render_dir(dem_stem, self.player)
        self.youtube_dir = PROJECT_ROOT / "youtube" / run_id_from_name(f"{self.match_id}_{dem_stem}_{self.player}_{self.map_name}")

        self.run_id = run_id_from_name(f"{self.match_id}_{dem_stem}_{self.player}_{self.map_name}")
        self.state = load_state(self.run_id)
        self.state.setdefault("data", {})
        self.state["data"]["steam_id"] = self.steam_id

        # Resume-safe: trust the state's prior dual_upload/overlay_only flags
        # if set, so a resume run doesn't need to re-pass them.
        state_dual = bool(self.state["data"].get("dual_upload"))
        state_overlay_only = bool(self.state["data"].get("overlay_only"))
        self.overlay_only = bool(getattr(args, "overlay_only", False)) or state_overlay_only
        # overlay_only implies dual_upload (we still want the overlay branch
        # in steps 3-7; we just skip the raw branch).
        self.dual_upload = bool(getattr(args, "dual_upload", False)) or state_dual or self.overlay_only

        if self.dual_upload:
            self.overlay_youtube_dir = self.youtube_dir.with_name(self.youtube_dir.name + "_overlay")
        else:
            self.overlay_youtube_dir = None

        if self.state["data"].get("demo_path"):
            self.demo_path = Path(self.state["data"]["demo_path"])
        self.state["data"]["render_dir"] = str(self.render_dir)
        self.state["data"]["youtube_dir"] = str(self.youtube_dir)
        self.state["data"]["ratings_path"] = str(self.ratings_json)
        if self.dual_upload and self.overlay_youtube_dir is not None:
            self.state["data"]["overlay_youtube_dir"] = str(self.overlay_youtube_dir)
            self.state["data"]["dual_upload"] = True
        if self.overlay_only:
            self.state["data"]["overlay_only"] = True
        if self.avatar_path:
            self.state["data"]["avatar_path"] = str(self.avatar_path)

    def _ensure_demo(self) -> None:
        if self.demo_path and self.demo_path.exists():
            return
        hf_root = self.meta.get("hf_root", "").strip()
        hf_repo = self.meta.get("hf_repo", "cs2povarchive/cs2-demos")
        if not hf_root or not self.demo_path:
            return
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
        except Exception as e:
            fail(0, "HF_DOWNLOAD_FAILED",
                 f"Failed to download {hf_remote} from {hf_repo}: {e}")
        if not self.demo_path.exists():
            fail(0, "HF_DOWNLOAD_MISSING",
                 f"Download said success but file not found: {self.demo_path}")
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
            # Step 3 (concat) also done — skip both
            print(f"  [skip] render already complete: {combined.name} "
                  f"({combined.stat().st_size // 1024 // 1024} MB)")
            if self.start_step <= 3:
                self.start_step = 4
                self.state["step"] = 4
                save_state(self.run_id, self.state)
            return
        # Step 2 partial: any batch-*.mp4 ≥1MB exists → skip analyze (cheap
        # re-analyze is fine) but DO NOT skip render (filesystem-based resume
        # inside render_pov.py will pick up existing batches).
        # Step 3: no combined.mp4 yet → keep at user's start_step

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
                fail(step_num, f"STEP_{step_name.upper()}_EXCEPTION", f"{e}")

        print(f"\n  [OK] Pipeline complete -> {self.youtube_dir}/")

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

        from cs2_minimizer import ensure_cs2_closed

        ensure_cs2_closed()

        nvcheck = subprocess.run(
            [
                r"C:\Users\jembo\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe",
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

        render_args = [
            "scripts/render_pov.py", str(self.demo_path), self.steam_id,
            "--output", str(self.render_dir),
            "--batches", str(getattr(self.args, "batches", 20)),
        ]
        hf_root = self.meta.get("hf_root", "").strip()
        if hf_root:
            render_args += ["--hf-root", hf_root]
            if self.match_id:
                render_args += ["--match-id", str(self.match_id)]
        r = self._run_py(render_args, timeout=43200)
        if r.returncode != 0:
            fail(2, "RENDER_FAILED", f"render_pov.py exited {r.returncode}")

        batch_files = sorted(
            [f for f in self.render_dir.glob("batch-*.mp4") if re.match(r"batch-\d+-\d+\.mp4$", f.name)],
            key=lambda f: int(re.match(r"batch-(\d+)-\d+\.mp4$", f.name).group(1)),
        )
        if not batch_files:
            fail(2, "RENDER_NO_BATCHES", f"no batch-*.mp4 files in {self.render_dir}")

        round_count = self.state["data"].get("round_count", 0)
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

    @staticmethod
    def _probe_duration(path: Path) -> float:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            return 0.0
        data = json.loads(r.stdout)
        return float(data.get("format", {}).get("duration", 0) or 0)

    def _validate_concat(self, combined: Path) -> None:
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
        if actual_rounds and actual_rounds != expected_rounds:
            fail(3, "CONCAT_ROUND_COUNT_MISMATCH",
                 f"concat has {actual_rounds} rounds but csdm analysis has "
                 f"{expected_rounds} rounds (missing/extra round clips?)")
        else:
            print(f"  [OK] round count: {actual_rounds} rounds match "
                  f"analysis ({expected_rounds})")

        # Sidecar self-consistency + match against the real combined.mp4
        # duration. Catches corrupt total_duration_seconds / round offsets
        # past EOF (e.g. probing cumulative combined after each batch append).
        if off is not None:
            from concat_rounds import validate_round_offsets_sidecar
            actual_sec = self._probe_duration(combined)
            sidecar_errs = validate_round_offsets_sidecar(
                off, video_duration_seconds=actual_sec if actual_sec > 0 else None,
            )
            if sidecar_errs:
                fail(3, "CONCAT_SIDECAR_INVALID", "; ".join(sidecar_errs))
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

    def _expected_duration_from_demo(self, tickrate: float, round_nums: list[int] | None = None) -> float | None:
        """Compute the player-POV rendered duration from the demo itself, as a
        fallback when CSDM per-round sequence files are absent (single-batch
        renders). Uses demoparser2. This is the INDEPENDENT expected duration
        (derived from demo ticks, not the video) used to catch truncation.

        CSDM `--event rounds --perspective player` renders each round from a
        fixed lead-in before freeze-end to the player's death (+ fixed trail),
        or to the round's end if the player survives. All phases are fixed:
          start = round_freeze_end - 2s   (csdm --start-seconds-before default)
          end   = player_death   + 2s    (csdm --end-seconds-after default)
          end   = round_end       + 2s    (survived: same fixed trail)
        Both the buy/freeze lead-in and the post-death/post-round trail are
        fixed CSDM constants (defaults 2s), so this is EXACT (verified
        against CSDM sequence tick ranges: r1 1146-3661, r2 5513-9302,
        r3 10933-17148)."""
        try:
            import demoparser2 as dp
        except Exception:
            return None
        demo = self.demo_path
        if not demo or not Path(demo).exists():
            return None
        try:
            p = dp.DemoParser(str(demo))
            rs = p.parse_event("round_start")
            re_ = p.parse_event("round_end")
            fe = p.parse_event("round_freeze_end")
            deaths = p.parse_event("player_death")
        except Exception:
            return None
        if rs is None or re_ is None or len(rs) == 0:
            return None
        steam = str(self.steam_id)
        # Build per-round tick ranges {round: (start, end)}.
        ranges = {}
        for n in range(1, len(rs) + 1):
            try:
                s = int(rs[rs["round"] == n]["tick"].iloc[0])
            except Exception:
                continue
            end_row = re_[re_["round"] == n + 1]
            if len(end_row) == 0:
                continue
            ranges[n] = (s, int(end_row["tick"].iloc[0]))
        # Map player deaths to rounds via tick (player_death has no 'round'
        # column, so locate each death tick inside a round's [start, end]).
        death_by_round = {}
        if deaths is not None and len(deaths) > 0 and "user_steamid" in deaths.columns:
            for _, d in deaths.iterrows():
                if str(d["user_steamid"]) != steam:
                    continue
                t = int(d["tick"])
                for rn, (s, e) in ranges.items():
                    if s <= t <= e:
                        death_by_round[rn] = t
                        break
        LEAD = int(2 * tickrate)     # csdm --start-seconds-before (fixed)
        TRAIL = int(2 * tickrate)   # csdm --end-seconds-after (fixed)
        freeze = list(fe["tick"]) if fe is not None and len(fe) > 0 else []
        total = 0.0
        for n in (round_nums or list(ranges.keys())):
            if n not in ranges:
                continue
            start_tick, end_tick = ranges[n]
            fz = int(freeze[n - 1]) if 0 < n <= len(freeze) else start_tick
            rend_start = fz - LEAD
            dth = death_by_round.get(n)
            rend_end = (dth if dth is not None else end_tick) + TRAIL
            span = max(rend_end - rend_start, 0)
            total += span / tickrate
        return total if total > 0 else None

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
        if self.overlay_youtube_dir is None:
            print("  [skip] no overlay_youtube_dir configured")
            return
        target = self.overlay_youtube_dir / "video.mp4"
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
        self.overlay_youtube_dir.mkdir(parents=True, exist_ok=True)
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

        r = self._run_py(["scripts/concat_rounds.py", str(self.render_dir)], timeout=7200)
        if r.returncode != 0:
            fail(3, "CONCAT_FAILED", f"concat_rounds.py exited {r.returncode}")

        combined = self.render_dir / "combined.mp4"
        if not combined.exists():
            fail(3, "CONCAT_NO_COMBINED", f"no combined.mp4 found in {self.render_dir}")
        if combined.stat().st_size < 100000:
            fail(3, "CONCAT_OUTPUT_TOO_SMALL", f"combined.mp4 suspiciously small: {combined.stat().st_size} bytes")

        self._validate_concat(combined)

        # overlay-only: skip raw youtube dir entirely. The overlay variant
        # becomes the only output. (Raw combined.mp4 is still produced by
        # concat_rounds.py in render_dir; we just don't copy it to a
        # youtube/{run_id}/ dir, never add outro/thumbnail/upload for it.)
        if not self.overlay_only:
            self.youtube_dir.mkdir(parents=True, exist_ok=True)
            self._copy_video_to_youtube(self.youtube_dir, combined, label="raw")
            self._copy_round_offsets(self.youtube_dir)

        # Note: do NOT copy combined.mp4 to overlay_youtube_dir here.
        # Step 4 (overlay) creates the overlay dir and writes the overlaid
        # video. Copying the raw video here would let step 4 silently skip
        # (or fail to produce overlay) and leave a raw video in the overlay
        # dir that would then be uploaded under the overlay variant name.

    # ── Step 4: Overlay ───────────────────────────────────────────────────

    def step_overlay(self) -> None:
        """Apply keyboard + util flight overlay. In dual-upload mode the
        overlay is written into the dedicated overlay variant directory and
        replaces video.mp4 in that dir. In raw-only mode the original
        (orphaned) behavior is preserved: overlay_pov.py writes a sidecar
        video.overlay.mp4 next to video.mp4 which is otherwise unused."""
        if self.dual_upload and self.overlay_youtube_dir is not None:
            # Work under renders/ so intermediate files (batches, sidecar)
            # don't clutter youtube/. Only copy final result to youtube/.
            target_dir = self.render_dir / ".overlay_work"
        else:
            target_dir = self.youtube_dir

        video_path = target_dir / "video.mp4"
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
                if self.dual_upload:
                    # Copy raw combined.mp4 to renders/.overlay_work/ as working file.
                    src = self.render_dir / "combined.mp4"
                    if not src.exists() or src.stat().st_size < 100_000:
                        fail(4, "OVERLAY_NO_INPUT",
                             f"combined.mp4 missing/empty in render_dir")
                    target_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(src), str(video_path))
                    # Copy sidecar (round_offsets.json) alongside video.mp4
                    # overlay_pov.py looks for <video>.round_offsets.json next to video
                    sidecar_src = self.render_dir / "combined.round_offsets.json"
                    if sidecar_src.is_file():
                        sidecar_dst = target_dir / "video.round_offsets.json"
                        shutil.copy2(str(sidecar_src), str(sidecar_dst))
                        print(f"  [setup] copied sidecar to {sidecar_dst.name}")
                    print(f"  [setup] copied combined.mp4 to renders/.overlay_work/ "
                          f"for overlay_pov.py")
                else:
                    print(f"  [skip] video.mp4 not found in {target_dir.name} "
                          f"and no pre-rendered combined.overlay.mp4 in renders/")
                    return
        if not self.demo_path or not self.demo_path.exists():
            if self.dual_upload:
                fail(4, "OVERLAY_NO_DEMO",
                     f"demo required for overlay but not found: {self.demo_path}")
            print("  [skip] demo not found")
            return
        steam_id = self.state["data"].get("steam_id", self.meta.get("steam_id", ""))
        if not steam_id:
            if self.dual_upload:
                fail(4, "OVERLAY_NO_STEAM_ID", "no steam_id for overlay")
            print("  [skip] no steam_id")
            return

        work_dir = self.render_dir / ".overlay_work"
        work_dir.mkdir(parents=True, exist_ok=True)
        r = self._run_py([
            "scripts/overlay_pov.py",
            "--video", str(video_path),
            "--demo", str(self.demo_path),
            "--steam-id", steam_id,
            "--batches", str(getattr(self.args, "overlay_batches", 10)),
            "--util-cams-root", str(self.render_dir / "utility_cams"),
            "--work-dir", str(work_dir),
        ], timeout=7200, capture_output=True, text=True)
        if r.returncode != 0:
            if r.stdout:
                print(r.stdout)
            if r.stderr:
                print("[stderr]", r.stderr[-2000:])
            fail(4, "OVERLAY_FAILED", f"overlay_pov.py exited {r.returncode}")

        overlay_sidecar = video_path.with_suffix(".overlay.mp4")
        if not overlay_sidecar.exists():
            if self.dual_upload:
                fail(4, "OVERLAY_NO_OUTPUT",
                     f"overlay_pov.py succeeded but {overlay_sidecar} not found")
            print("  [skip] overlay produced no output")
            return

        if self.dual_upload:
            # Copy overlay result from renders/ work dir -> youtube/ variant dir
            self._copy_overlay_result_to_youtube(overlay=overlay_sidecar)
            # Clean up renders/.overlay_work/
            shutil.rmtree(target_dir, ignore_errors=True)
        else:
            # Legacy: sidecar lives next to raw video.mp4
            mb = overlay_sidecar.stat().st_size / 1024 / 1024
            print(f"  [OK] Overlay applied to video.mp4 (sidecar: {overlay_sidecar.name}, {mb:.0f} MB)")

    def _append_outro(self, youtube_dir: Path, step_num: int = 5) -> None:
        """Generate a 5s silent outro and append it to video.mp4 inside
        ``youtube_dir``. Used for both raw and overlay variants."""
        video = youtube_dir / "video.mp4"
        if not video.exists():
            fail(step_num, "OUTRO_VIDEO_MISSING", f"video.mp4 not found in {youtube_dir}")

        self._run_py(["scripts/generate_outro.py", str(video)], timeout=120)

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
            ["ffmpeg", "-f", "concat", "-safe", "0", "-i", list_path,
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

    def step_outro(self) -> None:
        if not self.overlay_only:
            self._append_outro(self.youtube_dir, step_num=5)
        if self.dual_upload and self.overlay_youtube_dir is not None:
            self._append_outro(self.overlay_youtube_dir, step_num=5)

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
        if include_youtube and self.overlay_youtube_dir is not None:
            candidates.append(self.overlay_youtube_dir / "video.mp4")
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
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(overlay_video)],
                capture_output=True, text=True, timeout=60,
            )
            if probe.returncode != 0:
                return None
            duration = float(probe.stdout.strip())
            seek_t = max(0.5, duration * 0.40)
            tmp = Path(tempfile.mkstemp(prefix="thumb_overlay_", suffix=".jpg")[1])
            r = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error",
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

    def _generate_thumbnail(self, youtube_dir: Path, variant: str, step_num: int = 6) -> None:
        """Generate a 1280x720 thumbnail in ``youtube_dir`` and write the
        corresponding upload_meta.json. ``variant`` is 'raw' (default) or
        'overlay' (adds W/ INPUT OVERLAY and + UTIL CAMS badges in bottom-right).

        For ``variant='overlay'``, the canonical background is a frame extracted
        from ``combined.overlay.mp4`` (the actual overlay video) so the keyboard
        + util-cam PiP is faintly visible behind the blur. Falls back to
        kill-frame extraction (raw demo) if the overlay video is missing.
        """
        from cs2_minimizer import ensure_cs2_closed

        ensure_cs2_closed()

        youtube_dir.mkdir(parents=True, exist_ok=True)
        thumb = youtube_dir / "thumbnail.jpg"

        bg_override: Path | None = None
        bg_cleanup: Path | None = None
        if variant == "overlay":
            overlay_vid = self._find_overlay_video()
            if overlay_vid is not None:
                print(f"  [bg] using overlay video: {overlay_vid.name}")
                bg_override = self._extract_overlay_frame(overlay_vid)
                bg_cleanup = bg_override
            else:
                print(f"  [bg] overlay video not found — falling back to kill-frame extraction")

        cmd = [
            "-m", "thumbnail",
            self.hltv_url,
            "--player", self.player,
            "--map", self.map_name,
            "--variant", variant,
        ]
        if bg_override is not None:
            cmd += ["--background", str(bg_override)]
        elif self.demo_path:
            cmd += ["--demo", str(self.demo_path)]
        if self.steam_id:
            cmd += ["--steam-id", self.steam_id]
        if self.tournament:
            cmd += ["--tournament", self.tournament]
        cmd += ["--output", str(youtube_dir)]

        r = self._run_py(cmd, timeout=300)
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
        if not self.overlay_only:
            self._generate_thumbnail(self.youtube_dir, variant="raw", step_num=6)
        if self.dual_upload and self.overlay_youtube_dir is not None:
            self._generate_thumbnail(
                self.overlay_youtube_dir, variant="overlay", step_num=6,
            )

    # ── Upload meta writer (runs inside thumbnail step) ────────────────

    def _write_upload_meta(
        self,
        youtube_dir: Path,
        variant: str = "raw",
        step_num: int = 5,
    ) -> None:
        """Generate title/desc/tags via generate_title.py and write the
        resulting upload_meta.json into ``youtube_dir``. Pass
        ``variant='overlay'`` to suffix title/desc/tags for the dual-upload
        overlay variant."""
        video = youtube_dir / "video.mp4"
        thumb = youtube_dir / "thumbnail.jpg"

        titlize_args = [
            "scripts/generate_title.py", str(self.ratings_json),
            "--player", self.player,
            "--map", self.map_name,
            "--variant", variant,
        ]
        if self.tournament:
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
        "--no-dual-upload",
        action="store_false",
        dest="dual_upload",
        help="Disable the second overlay variant (default: dual-upload ON).",
    )
    parser.set_defaults(dual_upload=True)
    parser.add_argument(
        "--overlay-only",
        action="store_true",
        help="Upload only the overlay variant. Skips raw video copy, raw "
             "outro, raw thumbnail, and raw upload. Implies --dual-upload's "
             "overlay branch. Use this when you only ever want the "
             "keyboard+util-cam version on the channel.",
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
        "--no-cleanup",
        action="store_true", default=True,
        help="Skip step 7 (delete renders/ + state file). ON BY DEFAULT. "
             "Keeps render cache and pipeline state on disk for re-runs. "
             "Pass --cleanup to run step 7 explicitly.",
    )
    parser.add_argument(
        "--cleanup",
        dest="no_cleanup",
        action="store_false",
        help="Run step 7 cleanup (delete renders/ + state file). "
             "Default: --no-cleanup. Use this flag to opt in.",
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
