"""
E2E pipeline: backlog metadata -> analyze -> render -> concat -> thumbnail -> upload.
Reads all POV metadata from a backlog file.
Structured errors for agent parsing.
Auto-downloads missing demos from HuggingFace if hf_root is set.

Usage:
    python scripts/pipeline.py --backlog backlog/<match_slug>/<priority>/<slug>.json [--step N]

Steps (use --step N to start at a specific step):
  1 = analyze    csdm analyze the demo
  2 = render     Render all rounds as POV clips
  3 = concat     Concatenate rounds, copy to youtube/
  4 = outro      Generate 5s silent outro, concat onto video.mp4
  5 = thumbnail  Generate 1280x720 thumbnail
  6 = upload     Upload to YouTube
  7 = cleanup    Remove renders folder + pipeline state
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

from huggingface_hub import hf_hub_download
from assign_playlist import normalize_playlist_name
STATE_DIR = PROJECT_ROOT / ".pipeline"
STATE_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(str(PROJECT_ROOT))

CSDM = r"C:\Users\jembo\AppData\Local\Programs\cs-demo-manager\csdm.cmd"
PY = sys.executable

STEPS = {
    1: "analyze",
    2: "render",
    3: "concat",
    4: "outro",
    5: "thumbnail",
    6: "upload",
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
    return PROJECT_ROOT / "demos" / "renders" / f"pov-{dem_stem}_{player_slug}"


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

        self.start_step = args.step
        self.end_step = args.until if args.until is not None else max(STEPS.keys())

        from scrapers.hltv_acquire import match_id_from_url

        slug = self.hltv_url.rstrip("/").split("/")[-1]
        match_id = match_id_from_url(self.hltv_url)
        dem_stem = self.demo_path.stem if self.demo_path else slug
        self.render_dir = pov_render_dir(dem_stem, self.player)
        self.youtube_dir = PROJECT_ROOT / "youtube" / run_id_from_name(f"{match_id}_{dem_stem}_{self.player}_{self.map_name}")

        self.run_id = run_id_from_name(f"{match_id}_{dem_stem}_{self.player}_{self.map_name}")
        self.state = load_state(self.run_id)
        self.state.setdefault("data", {})
        self.state["data"]["steam_id"] = self.steam_id

        if self.state["data"].get("demo_path"):
            self.demo_path = Path(self.state["data"]["demo_path"])
        self.state["data"]["render_dir"] = str(self.render_dir)
        self.state["data"]["youtube_dir"] = str(self.youtube_dir)
        self.state["data"]["ratings_path"] = str(self.ratings_json)
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
        hf_remote = f"{hf_root}/{match_slug}/{dem_filename}"
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

    def run(self) -> None:
        if self.end_step < self.start_step:
            fail(0, "INVALID_STEP_RANGE", f"--until {self.end_step} is before --step {self.start_step}")
        self._ensure_demo()
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

    # ── Step 5: Analyze ──────────────────────────────────────────────────

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

    # ── Step 6: Render ───────────────────────────────────────────────────

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

    # ── Step 7: Concat ───────────────────────────────────────────────────

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

        self.youtube_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(combined), str(self.youtube_dir / "video.mp4"))
        vid_size = (self.youtube_dir / "video.mp4").stat().st_size
        print(f"  [OK] Copied video.mp4 ({vid_size / 1e9:.1f} GB)")

    # ── Step 8: Outro ────────────────────────────────────────────────────

    def step_outro(self) -> None:
        video = self.youtube_dir / "video.mp4"
        if not video.exists():
            fail(4, "OUTRO_VIDEO_MISSING", f"video.mp4 not found in {self.youtube_dir}")

        self._run_py(["scripts/generate_outro.py", str(video)], timeout=120)

        outro = self.youtube_dir / "outro.mp4"
        if not outro.exists():
            fail(4, "OUTRO_CLIP_MISSING", f"outro.mp4 not generated in {self.youtube_dir}")
        if outro.stat().st_size < 1000:
            fail(4, "OUTRO_TOO_SMALL", f"outro.mp4 too small: {outro.stat().st_size} bytes")

        temp = self.youtube_dir / "video.temp.mp4"
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
            fail(4, "OUTRO_CONCAT_FAILED", f"ffmpeg concat failed: {r.stderr[-300:]}")

        temp.replace(video)
        outro.unlink()
        vid_mb = video.stat().st_size / 1024 / 1024
        print(f"  [OK] Outro appended, video.mp4 ({vid_mb:.0f} MB)")

    # ── Step 9: Thumbnail ────────────────────────────────────────────────

    def step_thumbnail(self) -> None:
        from cs2_minimizer import ensure_cs2_closed

        ensure_cs2_closed()

        self.youtube_dir.mkdir(parents=True, exist_ok=True)
        thumb = self.youtube_dir / "thumbnail.jpg"

        cmd = [
            "-m", "thumbnail",
            self.hltv_url,
            "--player", self.player,
            "--map", self.map_name,
        ]
        if self.demo_path:
            cmd += ["--demo", str(self.demo_path)]
        if self.steam_id:
            cmd += ["--steam-id", self.steam_id]
        if self.tournament:
            cmd += ["--tournament", self.tournament]
        cmd += ["--output", str(self.youtube_dir)]

        r = self._run_py(cmd, timeout=300)
        if r.returncode != 0:
            fail(5, "THUMBNAIL_FAILED", f"thumbnail generator exited {r.returncode}")

        if not thumb.exists():
            fail(5, "THUMBNAIL_MISSING", f"thumbnail not created at {thumb}")
        if thumb.stat().st_size < 1000:
            fail(5, "THUMBNAIL_TOO_SMALL", f"thumbnail too small: {thumb.stat().st_size} bytes")

        try:
            from PIL import Image
            im = Image.open(thumb)
            if im.size != (1280, 720):
                fail(5, "THUMBNAIL_BAD_SIZE", f"thumbnail dimensions {im.size} != expected 1280x720")
        except ImportError:
            pass

        print(f"  [OK] Thumbnail: {thumb.name}")
        self._write_upload_meta()

    # ── Step 10: Upload ─────────────────────────────────────────────────

    def _write_upload_meta(self) -> None:
        video = self.youtube_dir / "video.mp4"
        thumb = self.youtube_dir / "thumbnail.jpg"

        titlize_args = [
            "scripts/generate_title.py", str(self.ratings_json),
            "--player", self.player,
            "--map", self.map_name,
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
        }

        meta_path = self.youtube_dir / "upload_meta.json"
        meta_path.write_text(json.dumps(upload_meta, indent=2))
        print(f"  [OK] upload_meta.json written")

    def step_upload(self) -> None:
        video = self.youtube_dir / "video.mp4"
        thumb = self.youtube_dir / "thumbnail.jpg"

        if not video.exists():
            fail(6, "UPLOAD_VIDEO_MISSING", f"video not found: {video}")
        if video.stat().st_size < 100000:
            fail(6, "UPLOAD_VIDEO_TOO_SMALL", f"video too small: {video.stat().st_size} bytes")

        meta_path = self.youtube_dir / "upload_meta.json"
        if not meta_path.exists():
            self._write_upload_meta()

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
            fail(6, "UPLOAD_FAILED", f"upload exited {proc.returncode}: {out[:300]}")

        m = re.search(r"https://youtu\.be/([a-zA-Z0-9_-]+)", out)
        if m:
            vid_id = m.group(1)
            self.state["data"]["youtube_id"] = vid_id
            print(f"  [OK] Uploaded: https://youtu.be/{vid_id}")
        else:
            fail(6, "UPLOAD_NO_VIDEO_ID", f"could not extract video ID from output: {out[:200]}")

    # ── Step 11: Cleanup ────────────────────────────────────────────────

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
            fail(7, "CLEANUP_RENDER_DIR_FAILED", f"render dir still exists after rmtree: {self.render_dir}")
        if state_path.exists():
            fail(7, "CLEANUP_STATE_FAILED", f"pipeline state file still exists: {state_path}")
        if not removed:
            print("  Nothing to clean up")
        else:
            print(f"  [OK] Removed {len(removed)} item(s)")


def main() -> None:
    parser = argparse.ArgumentParser(description="E2E Pipeline: backlog .md -> render -> thumbnail -> upload")
    parser.add_argument("--backlog", required=True, help="Path to backlog markdown file with BACKLOG_META")
    parser.add_argument("--step", type=int, default=1, choices=range(1, 8),
                        help="Start from step N (1=analyze..7=cleanup)")
    parser.add_argument("--until", type=int, default=None, choices=range(1, 7),
                        help="Stop after step N (default: 7)")
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
