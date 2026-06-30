"""
E2E pipeline: backlog metadata -> analyze -> render -> concat -> thumbnail -> upload.
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
  6 = thumbnail  Generate 1280x720 thumbnail
  7 = upload     Upload to YouTube
  8 = cleanup    Remove renders folder + pipeline state

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
    7: "upload",
    8: "cleanup",
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
        # --no-cleanup caps end_step at 7 (skip step 8).
        if getattr(args, "no_cleanup", True) and self.end_step == max(STEPS.keys()):
            self.end_step = 7

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

    def run(self) -> None:
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
        self, youtube_dir: Path, source: Path, offsets_src: Path | None,
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

        # overlay-only: skip raw youtube dir entirely. The overlay variant
        # becomes the only output. (Raw combined.mp4 is still produced by
        # concat_rounds.py in render_dir; we just don't copy it to a
        # youtube/{run_id}/ dir, never add outro/thumbnail/upload for it.)
        if not self.overlay_only:
            self.youtube_dir.mkdir(parents=True, exist_ok=True)
            self._copy_video_to_youtube(self.youtube_dir, combined, label="raw")
            self._copy_round_offsets(self.youtube_dir)

        # Dual-upload: also copy raw combined into the overlay variant dir.
        # Step 4 will overwrite this with the overlay-enhanced version.
        if self.dual_upload and self.overlay_youtube_dir is not None:
            self.overlay_youtube_dir.mkdir(parents=True, exist_ok=True)
            self._copy_video_to_youtube(self.overlay_youtube_dir, combined, label="overlay")
            self._copy_round_offsets(self.overlay_youtube_dir)

    # ── Step 4: Overlay ───────────────────────────────────────────────────

    def step_overlay(self) -> None:
        """Apply keyboard + util flight overlay. In dual-upload mode the
        overlay is written into the dedicated overlay variant directory and
        replaces video.mp4 in that dir. In raw-only mode the original
        (orphaned) behavior is preserved: overlay_pov.py writes a sidecar
        video.overlay.mp4 next to video.mp4 which is otherwise unused."""
        if self.dual_upload and self.overlay_youtube_dir is not None:
            target_dir = self.overlay_youtube_dir
        else:
            target_dir = self.youtube_dir

        video_path = target_dir / "video.mp4"
        if not video_path.exists():
            # Auto-recover: look for a pre-rendered combined.overlay.mp4 in
            # any known render-dir convention (e.g. an external overlay pass
            # in a separate render dir, with step 3 skipped on resume). Copy
            # THAT as the overlay video. NEVER fall back to copying the raw
            # video — that would upload the un-overlaid POV under the overlay
            # variant on a resume that skips step 4.
            external = self._find_overlay_video()
            if external is not None and external.exists():
                target_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(external), str(video_path))
                print(f"  [recover] copied pre-rendered overlay "
                      f"({external.stat().st_size / 1e9:.1f} GB) from "
                      f"{external.parent.name}/{external.name} -> overlay dir; "
                      f"skipping overlay_pov.py (already overlaid)")
                return
            else:
                print(f"  [skip] video.mp4 not found in {target_dir.name} "
                      f"and no pre-rendered combined.overlay.mp4 in renders/")
                return
        if not self.demo_path or not self.demo_path.exists():
            print("  [skip] demo not found")
            return
        steam_id = self.state["data"].get("steam_id", self.meta.get("steam_id", ""))
        if not steam_id:
            print("  [skip] no steam_id")
            return

        r = self._run_py([
            "scripts/overlay_pov.py",
            "--video", str(video_path),
            "--demo", str(self.demo_path),
            "--steam-id", steam_id,
            "--batches", str(getattr(self.args, "overlay_batches", 10)),
            "--util-cams-root", str(self.render_dir / "utility_cams"),
        ], timeout=7200)
        if r.returncode != 0:
            fail(4, "OVERLAY_FAILED", f"overlay_pov.py exited {r.returncode}")

        overlay_sidecar = video_path.with_suffix(".overlay.mp4")
        if not overlay_sidecar.exists():
            # Overlay script did not produce a sidecar (e.g. nothing to overlay).
            if self.dual_upload:
                fail(4, "OVERLAY_NO_OUTPUT",
                     f"overlay_pov.py succeeded but {overlay_sidecar} not found")
            print("  [skip] overlay produced no output")
            return

        if self.dual_upload:
            # Replace raw video.mp4 in the overlay dir with the overlaid version.
            video_path.unlink()
            shutil.move(str(overlay_sidecar), str(video_path))
            mb = video_path.stat().st_size / 1024 / 1024
            print(f"  [OK] Overlay applied to {target_dir.name}/video.mp4 ({mb:.0f} MB)")
        else:
            # Legacy behavior: sidecar left orphaned next to raw video.mp4.
            mb = overlay_sidecar.stat().st_size / 1024 / 1024
            print(f"  [OK] Overlay applied to video.mp4 (sidecar: {overlay_sidecar.name}, {mb:.0f} MB)")

        # Cleanup any leftover batch-overlay-*.mp4 files (should already be
        # removed by overlay_pov.py on success, but defensive cleanup in case
        # of partial success or interrupted run).
        for bf in target_dir.glob("batch-overlay-*.mp4"):
            bf.unlink(missing_ok=True)

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

    def _find_overlay_video(self) -> Path | None:
        """Locate ``combined.overlay.mp4`` for this POV across known conventions.

        Search order:
          1. ``renders/pov-{dem_stem}_{steam_id}_full/combined.overlay.mp4``
          2. ``renders/pov-{dem_stem}_{player}_full/combined.overlay.mp4``
          3. ``renders/pov-{dem_stem}_{player_slug}/combined.overlay.mp4`` (standard render dir)
        """
        if not self.demo_path or not self.steam_id:
            return None
        dem_stem = self.demo_path.stem
        player_slug = run_id_from_name(self.player)
        candidates = [
            PROJECT_ROOT / "renders" / f"pov-{dem_stem}_{self.steam_id}_full" / "combined.overlay.mp4",
            PROJECT_ROOT / "renders" / f"pov-{dem_stem}_{self.player}_full" / "combined.overlay.mp4",
            PROJECT_ROOT / "renders" / f"pov-{dem_stem}_{player_slug}" / "combined.overlay.mp4",
        ]
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
            pass

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

    # ── Step 7: Upload ─────────────────────────────────────────────────

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

    def _upload_variant(
        self,
        youtube_dir: Path,
        variant: str,
        state_key: str,
        step_num: int = 7,
    ) -> None:
        """Upload a single variant (raw or overlay) to YouTube. Reads
        upload_meta.json, runs upload_youtube.py, captures the YouTube ID,
        and persists it under ``state_key`` in pipeline state.

        Resume-safe: if upload_meta.json already has a ``youtube_id`` or
        state already has one, the upload is skipped (variant was already
        uploaded on a prior run)."""
        video = youtube_dir / "video.mp4"
        thumb = youtube_dir / "thumbnail.jpg"

        meta_path = youtube_dir / "upload_meta.json"
        if not meta_path.exists():
            self._write_upload_meta(youtube_dir, variant=variant, step_num=step_num)

        # Resume: trust any youtube_id we already have (state OR meta). Sync
        # the meta to "completed" so a later run doesn't re-upload. Also
        # clears any stale resumable_* fields from a partial prior attempt
        # so upload_youtube.py can't accidentally resume a dead session.
        try:
            existing = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
        existing_id = existing.get("youtube_id") or self.state["data"].get(state_key)
        if existing_id:
            if existing.get("upload_status") != "completed":
                existing["youtube_id"] = existing_id
                existing["upload_status"] = "completed"
                existing.pop("resumable_uri", None)
                existing.pop("resumable_progress", None)
                existing.pop("video_size", None)
                meta_path.write_text(json.dumps(existing, indent=2))
            self.state["data"][state_key] = existing_id
            print(f"  [skip] {variant} already uploaded: https://youtu.be/{existing_id}")
            return

        if not video.exists():
            fail(step_num, "UPLOAD_VIDEO_MISSING",
                 f"[{variant}] video not found: {video}")
        if video.stat().st_size < 100000:
            fail(step_num, "UPLOAD_VIDEO_TOO_SMALL",
                 f"[{variant}] video too small: {video.stat().st_size} bytes")

        cmd = [
            "scripts/upload_youtube.py",
            str(video),
            "--meta", str(meta_path),
            "--privacy", "private",
        ]
        if thumb.exists():
            cmd += ["--thumbnail", str(thumb)]

        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        proc = subprocess.Popen(
            [PY] + cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=env, text=True, encoding="utf-8",
        )
        out_lines: list[str] = []
        if proc.stdout:
            for line in proc.stdout:
                print(line, end="")
                out_lines.append(line)
        proc.wait()
        out = "".join(out_lines)

        if proc.returncode != 0:
            fail(step_num, "UPLOAD_FAILED",
                 f"[{variant}] upload exited {proc.returncode}: {out[:300]}")

        m = re.search(r"https://youtu\.be/([a-zA-Z0-9_-]+)", out)
        if m:
            vid_id = m.group(1)
            self.state["data"][state_key] = vid_id
            print(f"  [OK] [{variant}] Uploaded: https://youtu.be/{vid_id}")
        else:
            fail(step_num, "UPLOAD_NO_VIDEO_ID",
                 f"[{variant}] could not extract video ID: {out[:200]}")

    def step_upload(self) -> None:
        if not self.overlay_only:
            self._upload_variant(
                self.youtube_dir, variant="raw", state_key="youtube_id", step_num=7,
            )
        if self.dual_upload and self.overlay_youtube_dir is not None:
            self._upload_variant(
                self.overlay_youtube_dir, variant="overlay",
                state_key="overlay_youtube_id", step_num=7,
            )

    # ── Step 8: Cleanup ────────────────────────────────────────────────

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
    parser.add_argument("--step", type=int, default=None, choices=range(1, 9),
                        help="Start from step N (1=analyze..8=cleanup). "
                             "Default: auto-resume from last completed step.")
    parser.add_argument("--until", type=int, default=None, choices=range(1, 8),
                        help="Stop after step N (default: 7)")
    parser.add_argument(
        "--dual-upload",
        action="store_true",
        help="Produce and upload a second, independent overlay variant "
             "(separate youtube dir, title, thumbnail, description).",
    )
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
        help="Skip step 8 (delete renders/ + state file). ON BY DEFAULT. "
             "Keeps render cache and pipeline state on disk for re-runs. "
             "Pass --cleanup to run step 8 explicitly.",
    )
    parser.add_argument(
        "--cleanup",
        dest="no_cleanup",
        action="store_false",
        help="Run step 8 cleanup (delete renders/ + state file). "
             "Default: --no-cleanup. Use this flag to opt in.",
    )
    args = parser.parse_args()

    meta = _parse_backlog(args.backlog)
    print(f"{'='*60}")
    print(f"  CS2Archive Pipeline")
    print(f"  Player: {meta.get('player', '?')} | Map: {meta.get('map', '?')}")
    print(f"  Demo:   {meta.get('demo_path', '(unknown)')}")
    print(f"  Step:   {args.step}" + (f" -> {args.until}" if args.until else f" -> 7"))
    print(f"{'='*60}")

    Pipeline(args).run()


if __name__ == "__main__":
    main()
