"""
E2E pipeline: .rar/.dem -> analyze -> render -> concat -> thumbnail -> upload.
Structured errors for agent parsing.

Usage:
    python scripts/pipeline.py <player> <map> <hltv_url> --steam-id <id> --demo <dem_path> [options]

Steps (use --step N to start at a specific step):
  1 = acquire     Download from HLTV (CloakBrowser), extract, pick .dem for map
  2 = ratings     Scrape HLTV Rating 3.0
  3 = steam_id    Save player Steam64 to player list
  4 = avatar      Download player cutout PNG from HLTV
  5 = analyze     csdm analyze the demo
  6 = render      Render all rounds as POV clips
  7 = concat      Concatenate rounds, copy to youtube/
  8 = outro       Generate 5s silent outro, concat onto video.mp4
  9 = thumbnail   Generate 1280x720 thumbnail
  10 = upload     Upload to YouTube (--privacy, auto-generates title + description)
  11 = cleanup    Remove renders folder + pipeline state
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
from youtube_schedule import AUTO_PUBLISH_MODE
STATE_DIR = PROJECT_ROOT / ".pipeline"
STATE_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(str(PROJECT_ROOT))

CSDM = r"C:\Users\jembo\AppData\Local\Programs\cs-demo-manager\csdm.cmd"
PY = sys.executable

STEPS = {
    1: "acquire",
    2: "ratings",
    3: "steam_id",
    4: "avatar",
    5: "analyze",
    6: "render",
    7: "concat",
    8: "outro",
    9: "thumbnail",
    10: "upload",
    11: "cleanup",
}


def pipeline_error(step: int, code: str, message: str) -> str:
    """Return structured JSON error line for agent parsing."""
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
    """Per-POV render folder — one demo can produce multiple player POVs."""
    player_slug = run_id_from_name(player)
    return PROJECT_ROOT / "demos" / "renders" / f"pov-{dem_stem}_{player_slug}"


def _find_steam_cmd() -> str | None:
    for p in [r"C:\Program Files (x86)\Steam\steam.exe", r"C:\Program Files\Steam\steam.exe"]:
        if Path(p).exists():
            return p
    return None


class Pipeline:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.steam_id = args.steam_id
        self.start_step = args.step
        self.end_step = args.until if args.until is not None else max(STEPS.keys())

        slug = args.hltv_url.rstrip("/").split("/")[-1]
        self.ratings_json = PROJECT_ROOT / "demos" / "analysis" / f"{slug}_ratings.json"

        self.demo_path: Path | None = None
        self.demo_override: Path | None = None
        if args.demo:
            src = Path(args.demo)
            if src.suffix.lower() not in (".dem", ".rar"):
                fail(0, "INVALID_DEMO_PATH", f"Unsupported file: {src.suffix} (use .rar or .dem)")
            self.demo_override = src
            if src.suffix.lower() == ".dem":
                self.demo_path = src

        from scrapers.hltv_acquire import match_id_from_url, match_slug_from_url

        stem = (
            self.demo_path.stem
            if self.demo_path
            else (self.demo_override.stem if self.demo_override else match_slug_from_url(args.hltv_url))
        )
        match_id = match_id_from_url(args.hltv_url)
        self.run_id = run_id_from_name(f"{match_id}_{stem}_{args.player}_{args.map}")
        self.state = load_state(self.run_id)
        self.state.setdefault("data", {})

        if self.state["data"].get("demo_path"):
            self.demo_path = Path(self.state["data"]["demo_path"])

        dp = self.state["data"].get("demo_path") or (str(self.demo_path) if self.demo_path else "")
        dem_stem = Path(dp).stem if dp else stem
        self.render_dir = pov_render_dir(dem_stem, args.player)
        self.youtube_dir = PROJECT_ROOT / "youtube" / self.run_id
        self.state["data"]["render_dir"] = str(self.render_dir)
        self.state["data"]["youtube_dir"] = str(self.youtube_dir)

    def run(self) -> None:
        if self.end_step < self.start_step:
            fail(0, "INVALID_STEP_RANGE", f"--until {self.end_step} is before --step {self.start_step}")
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

    def _run_py(self, args: list[str], **kwargs) -> subprocess.CompletedProcess:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        kwargs.setdefault("env", env)
        if kwargs.get("text") and "encoding" not in kwargs:
            kwargs["encoding"] = "utf-8"
        return subprocess.run([PY] + args, **kwargs)

    # ── Step 1: Acquire ────────────────────────────────────────────────────

    def step_acquire(self) -> None:
        from scrapers.hltv_acquire import DEFAULT_PROFILE_DIR, resolve_demo_for_pov

        profile_dir = Path(self.args.profile_dir) if self.args.profile_dir else DEFAULT_PROFILE_DIR

        try:
            self.demo_path = resolve_demo_for_pov(
                self.args.hltv_url,
                self.args.map,
                demo_override=self.demo_override,
                force=self.args.force,
                headless=self.args.headless,
                profile_dir=profile_dir,
            )
        except FileNotFoundError as e:
            fail(1, "ACQUIRE_DEMO_NOT_FOUND", str(e))
        except ValueError as e:
            msg = str(e)
            if "map" in msg.lower() and ".dem" in msg.lower():
                fail(1, "ACQUIRE_MAP_NOT_FOUND", msg)
            fail(1, "ACQUIRE_FAILED", msg)
        except Exception as e:
            fail(1, "ACQUIRE_DOWNLOAD_FAILED", str(e))

        self.state["data"]["demo_path"] = str(self.demo_path)
        self.render_dir = pov_render_dir(self.demo_path.stem, self.args.player)
        self.state["data"]["render_dir"] = str(self.render_dir)
        print(f"  [OK] Demo: {self.demo_path}")

        if not self.demo_path.exists():
            fail(1, "ACQUIRE_DEM_MISSING", f"dem file vanished after acquire: {self.demo_path}")
        if self.demo_path.stat().st_size < 1024:
            fail(1, "ACQUIRE_DEM_TOO_SMALL", f"dem suspiciously small: {self.demo_path.stat().st_size} bytes")

    # ── Step 2: Ratings ──────────────────────────────────────────────────

    def step_ratings(self) -> None:
        r = self._run_py(["main.py", "ratings", self.args.hltv_url], capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            fail(2, "RATINGS_SUBPROCESS_FAILED", f"ratings cmd failed: {r.stderr[:300]}")

        if not self.ratings_json.exists():
            fail(2, "RATINGS_JSON_MISSING", f"ratings JSON not created: {self.ratings_json}")

        raw = self.ratings_json.read_text()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            fail(2, "RATINGS_JSON_INVALID", f"ratings JSON parse error: {e}")

        if not data.get("tables"):
            fail(2, "RATINGS_NO_TABLES", f"ratings JSON has no player stat tables: {self.ratings_json.name}")
        self.state["data"]["ratings_path"] = str(self.ratings_json)
        print(f"  [OK] Ratings: {self.ratings_json.name} ({len(data.get('tables', []))} tables)")

    # ── Step 3: Steam ID ─────────────────────────────────────────────────

    def step_steam_id(self) -> None:
        r = self._run_py(["main.py", "player", "add", self.args.player, "--steam", self.steam_id],
                         capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            fail(3, "STEAM_ID_ADD_FAILED", f"player add cmd failed: {r.stderr[:300]}")
        self.state["data"]["steam_id"] = self.steam_id

        # Verify by listing
        r2 = self._run_py(["main.py", "player", "list"], capture_output=True, text=True, timeout=10)
        if self.args.player not in r2.stdout:
            fail(3, "STEAM_ID_NOT_FOUND", f"player '{self.args.player}' not found in list after add")
        print(f"  [OK] Steam ID saved: {self.steam_id}")

    # ── Step 4: Avatar ───────────────────────────────────────────────────

    def step_avatar(self) -> None:
        from PIL import Image

        from scrapers.player_images import MIN_RES

        player_key = self.args.player.strip().lower()
        avatar_path = PROJECT_ROOT / "demos" / "avatars" / f"{player_key}.png"
        ratings_path = self.state["data"].get("ratings_path") or str(self.ratings_json)

        r = self._run_py(["-c", f"""
import asyncio
from pathlib import Path
from scrapers.player_images import fetch_avatar_for_player
asyncio.run(fetch_avatar_for_player(
    "{player_key}",
    "{self.args.hltv_url}",
    Path(r"{ratings_path}"),
    force={self.args.force_avatar!r},
))
"""], capture_output=True, text=True, timeout=120)
        out = (r.stdout or "") + (r.stderr or "")
        print(out[:500] if out else "  (no output)")

        if r.returncode != 0:
            fail(4, "AVATAR_FETCH_FAILED", f"avatar fetch exited {r.returncode}: {out[:300]}")

        if not avatar_path.exists():
            fail(4, "AVATAR_MISSING", f"avatar PNG not found: {avatar_path}")

        with Image.open(avatar_path) as im:
            w, h = im.size
        if w < MIN_RES or h < MIN_RES:
            fail(4, "AVATAR_TOO_SMALL", f"avatar PNG too small: {w}x{h} (min {MIN_RES}x{MIN_RES})")

        self.state["data"]["avatar_path"] = str(avatar_path)
        print(f"  [OK] Avatar: {avatar_path.name}")

    # ── Step 5: Analyze ──────────────────────────────────────────────────

    def step_analyze(self) -> None:
        if not self.demo_path or not self.demo_path.exists():
            fail(5, "ANALYZE_DEMO_MISSING", f"demo not found: {self.demo_path}")

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
            fail(5, "ANALYZE_FAILED", f"csdm analyze returned {r.returncode}: {combined[:300]}")

        # Verify csdm has data for this demo
        with tempfile.TemporaryDirectory() as tmp:
            r2 = subprocess.run([CSDM, "json", str(self.demo_path), "--output-folder", tmp],
                                capture_output=True, text=True, timeout=300)
            if r2.returncode != 0:
                fail(5, "ANALYZE_JSON_FAILED", f"csdm json export failed: {r2.stderr[:200]}")
            jf = list(Path(tmp).glob("*.json"))
            if not jf:
                fail(5, "ANALYZE_NO_JSON", "csdm json produced no output files")
            data = json.loads(jf[0].read_text(encoding="utf-8"))
            rounds = data.get("rounds", [])
            kills = data.get("kills", [])
            print(f"  [OK] Rounds: {len(rounds)}, Kills: {len(kills)}")
            if len(rounds) == 0:
                fail(5, "ANALYZE_NO_ROUNDS", "csdm analysis has zero rounds")
            self.state["data"]["round_count"] = len(rounds)

    # ── Step 6: Render ───────────────────────────────────────────────────

    def step_render(self) -> None:
        if not self.demo_path or not self.demo_path.exists():
            fail(6, "RENDER_DEMO_MISSING", f"demo not found: {self.demo_path}")

        from cs2_minimizer import ensure_cs2_closed

        ensure_cs2_closed()

        # Verify NVENC available before render
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
            fail(6, "RENDER_NO_NVENC",
                 f"h264_nvenc not available in ffmpeg. Install NVIDIA GPU drivers + NVENC-enabled ffmpeg.")

        # Verify Steam is running
        steam_check = subprocess.run(["tasklist", "/FI", "IMAGENAME eq steam.exe"],
                                     capture_output=True, text=True, timeout=10)
        if "steam.exe" not in steam_check.stdout:
            fail(6, "RENDER_STEAM_NOT_RUNNING", "Steam must be running before rendering")

        render_args = [
            "scripts/render_pov.py", str(self.demo_path), self.steam_id,
            "--output", str(self.render_dir),
            "--batches", str(self.args.batches),
        ]
        r = self._run_py(render_args, timeout=43200)
        if r.returncode != 0:
            fail(6, "RENDER_FAILED", f"render_pov.py exited {r.returncode}")

        batch_files = sorted(
            [f for f in self.render_dir.glob("batch-*.mp4") if re.match(r"batch-\d+-\d+\.mp4$", f.name)],
            key=lambda f: int(re.match(r"batch-(\d+)-\d+\.mp4$", f.name).group(1)),
        )
        if not batch_files:
            fail(6, "RENDER_NO_BATCHES", f"no batch-*.mp4 files in {self.render_dir}")

        round_count = self.state["data"].get("round_count", 0)
        if round_count > 0:
            last_batch = batch_files[-1]
            last_end = int(re.match(r"batch-\d+-(\d+)\.mp4$", last_batch.name).group(1))
            if last_end < round_count:
                fail(6, "RENDER_INCOMPLETE",
                     f"last batch ends at round {last_end}, expected {round_count}")

        total_mb = sum(f.stat().st_size for f in batch_files) / 1024 / 1024
        total_rounds = sum(
            int(re.match(r"batch-\d+-(\d+)\.mp4$", f.name).group(1))
            - int(re.match(r"batch-(\d+)-\d+\.mp4$", f.name).group(1))
            + 1
            for f in batch_files
        )
        print(f"  [OK] {len(batch_files)} batch(es), {total_rounds} round(s) ({total_mb:.0f} MB)")

    # ── Step 7: Concat ───────────────────────────────────────────────────

    def step_concat(self) -> None:
        if not self.render_dir.exists():
            fail(7, "CONCAT_RENDER_DIR_MISSING", f"render dir not found: {self.render_dir}")

        r = self._run_py(["scripts/concat_rounds.py", str(self.render_dir)], timeout=7200)
        if r.returncode != 0:
            fail(7, "CONCAT_FAILED", f"concat_rounds.py exited {r.returncode}")

        combined = self.render_dir / "combined.mp4"
        if not combined.exists():
            fail(7, "CONCAT_NO_COMBINED", f"no combined.mp4 found in {self.render_dir}")
        if combined.stat().st_size < 100000:
            fail(7, "CONCAT_OUTPUT_TOO_SMALL", f"combined.mp4 suspiciously small: {combined.stat().st_size} bytes")

        self.youtube_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(combined), str(self.youtube_dir / "video.mp4"))

        vid_size = (self.youtube_dir / "video.mp4").stat().st_size
        print(f"  [OK] Copied video.mp4 ({vid_size / 1e9:.1f} GB)")

    # ── Step 8: Outro ────────────────────────────────────────────────────

    def step_outro(self) -> None:
        video = self.youtube_dir / "video.mp4"
        if not video.exists():
            fail(8, "OUTRO_VIDEO_MISSING", f"video.mp4 not found in {self.youtube_dir}")

        self._run_py(["scripts/generate_outro.py", str(video)], timeout=120)

        outro = self.youtube_dir / "outro.mp4"
        if not outro.exists():
            fail(8, "OUTRO_CLIP_MISSING", f"outro.mp4 not generated in {self.youtube_dir}")
        if outro.stat().st_size < 1000:
            fail(8, "OUTRO_TOO_SMALL", f"outro.mp4 too small: {outro.stat().st_size} bytes")

        # Concat video + outro (stream copy, crash-safe via temp file)
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
            fail(8, "OUTRO_CONCAT_FAILED", f"ffmpeg concat failed: {r.stderr[-300:]}")

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
            self.args.hltv_url,
            "--player", self.args.player,
            "--map", self.args.map,
            "--demo", str(self.demo_path),
            "--steam-id", self.steam_id,
        ]
        cmd += ["--output", str(self.youtube_dir)]
        if self.args.tournament:
            cmd += ["--tournament", self.args.tournament]

        r = self._run_py(cmd, timeout=300)
        if r.returncode != 0:
            fail(9, "THUMBNAIL_FAILED", f"thumbnail generator exited {r.returncode}")

        if not thumb.exists():
            fail(9, "THUMBNAIL_MISSING", f"thumbnail not created at {thumb}")
        if thumb.stat().st_size < 1000:
            fail(9, "THUMBNAIL_TOO_SMALL", f"thumbnail too small: {thumb.stat().st_size} bytes")

        # Verify dimensions via Pillow
        try:
            from PIL import Image
            im = Image.open(thumb)
            if im.size != (1280, 720):
                fail(9, "THUMBNAIL_BAD_SIZE", f"thumbnail dimensions {im.size} != expected 1280x720")
        except ImportError:
            pass

        print(f"  [OK] Thumbnail: {thumb.name}")

        # Generate upload metadata for resume capability
        self._write_upload_meta()

    def _write_upload_meta(self) -> None:
        """Generate upload_meta.json with all metadata needed for upload resume."""
        video = self.youtube_dir / "video.mp4"
        thumb = self.youtube_dir / "thumbnail.jpg"

        r = self._run_py([
            "scripts/generate_title.py", str(self.ratings_json),
            "--player", self.args.player,
            "--map", self.args.map,
        ] + (["--tournament", self.args.tournament] if self.args.tournament else []),
            capture_output=True, text=True, timeout=15)

        meta = {}
        if r.returncode == 0 and r.stdout.strip():
            try:
                meta = json.loads(r.stdout.strip())
            except json.JSONDecodeError:
                pass

        upload_meta = {
            "title": meta.get("title") or f"{self.args.player} | {self.args.map}",
            "description": meta.get("description", ""),
            "tags": meta.get("tags", []),
            "video_path": str(video),
            "thumbnail_path": str(thumb) if thumb.exists() else None,
            "privacy": self.args.privacy,
            "playlist_name": normalize_playlist_name(self.args.tournament),
            "youtube_id": None,
            "upload_status": "pending",
        }
        if getattr(self.args, "publish_at", None):
            upload_meta["publish_at"] = self.args.publish_at
            upload_meta["publish_timezone"] = getattr(
                self.args, "publish_timezone", None
            ) or "Australia/Sydney"

        meta_path = self.youtube_dir / "upload_meta.json"
        meta_path.write_text(json.dumps(upload_meta, indent=2))
        print(f"  [OK] upload_meta.json written")
        self._write_upload_meta_shorts(upload_meta)

    def _write_upload_meta_shorts(self, upload_meta: dict) -> None:
        """Write upload_meta_shorts.json for optional Shorts upload."""
        title = upload_meta["title"]
        if "#shorts" not in title.lower():
            title = f"{title} #Shorts"
        desc = upload_meta.get("description", "")
        if "#shorts" not in desc.lower():
            desc = f"{desc.rstrip()}\n\n#Shorts" if desc.strip() else "#Shorts"
        tags = list(upload_meta.get("tags") or [])
        if "Shorts" not in tags:
            tags.append("Shorts")

        shorts_meta = {
            "title": title,
            "description": desc,
            "tags": tags,
            "video_path": str(self.youtube_dir / "short.mp4"),
            "privacy": upload_meta.get("privacy", self.args.privacy),
            "youtube_id": None,
            "upload_status": "pending",
        }
        publish_at = getattr(self.args, "publish_shorts_at", None) or getattr(
            self.args, "publish_at", None
        )
        if publish_at:
            shorts_meta["publish_at"] = publish_at
            shorts_meta["publish_timezone"] = getattr(
                self.args, "publish_timezone", None
            ) or "Australia/Sydney"

        shorts_path = self.youtube_dir / "upload_meta_shorts.json"
        shorts_path.write_text(json.dumps(shorts_meta, indent=2))
        print(f"  [OK] upload_meta_shorts.json written")

    # ── Step 10: Upload ─────────────────────────────────────────────────

    def step_upload(self) -> None:
        video = self.youtube_dir / "video.mp4"
        thumb = self.youtube_dir / "thumbnail.jpg"

        if not video.exists():
            fail(10, "UPLOAD_VIDEO_MISSING", f"video not found: {video}")
        if video.stat().st_size < 100000:
            fail(10, "UPLOAD_VIDEO_TOO_SMALL", f"video too small: {video.stat().st_size} bytes")

        meta_path = self.youtube_dir / "upload_meta.json"
        if not meta_path.exists():
            # Fallback: generate meta on the fly (legacy resume path)
            self._write_upload_meta()

        cmd = [
            "scripts/upload_youtube.py",
            str(video),
            "--meta", str(meta_path),
            "--privacy", self.args.privacy,
        ]
        if thumb.exists():
            cmd += ["--thumbnail", str(thumb)]
        if getattr(self.args, "publish_at", None):
            cmd += ["--publish-at", self.args.publish_at]
            cmd += [
                "--timezone",
                getattr(self.args, "publish_timezone", None) or "Australia/Sydney",
            ]

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
            fail(10, "UPLOAD_FAILED", f"upload exited {proc.returncode}: {out[:300]}")

        m = re.search(r"https://youtu\.be/([a-zA-Z0-9_-]+)", out)
        if m:
            vid_id = m.group(1)
            self.state["data"]["youtube_id"] = vid_id
            print(f"  [OK] Uploaded: https://youtu.be/{vid_id}")
        else:
            fail(10, "UPLOAD_NO_VIDEO_ID", f"could not extract video ID from output: {out[:200]}")

        self._upload_shorts_if_present()

    def _upload_shorts_if_present(self) -> None:
        short = self.youtube_dir / "short.mp4"
        if not short.exists():
            print("  [SKIP] No short.mp4 — Shorts upload skipped")
            return
        if short.stat().st_size < 10000:
            fail(10, "UPLOAD_SHORT_TOO_SMALL", f"short.mp4 too small: {short.stat().st_size} bytes")

        shorts_meta_path = self.youtube_dir / "upload_meta_shorts.json"
        if not shorts_meta_path.exists():
            if (self.youtube_dir / "upload_meta.json").exists():
                upload_meta = json.loads(
                    (self.youtube_dir / "upload_meta.json").read_text(encoding="utf-8")
                )
                self._write_upload_meta_shorts(upload_meta)
            else:
                fail(10, "UPLOAD_SHORTS_META_MISSING", "upload_meta_shorts.json not found")

        cmd = [
            "scripts/upload_youtube_shorts.py",
            str(short),
            "--meta", str(shorts_meta_path),
            "--privacy", self.args.privacy,
        ]
        publish_at = getattr(self.args, "publish_shorts_at", None) or getattr(
            self.args, "publish_at", None
        )
        if publish_at:
            cmd += ["--publish-at", publish_at]
            cmd += [
                "--timezone",
                getattr(self.args, "publish_timezone", None) or "Australia/Sydney",
            ]

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
            fail(10, "UPLOAD_SHORTS_FAILED", f"shorts upload exited {proc.returncode}: {out[:300]}")

        m = re.search(r"https://youtu\.be/([a-zA-Z0-9_-]+)", out)
        if m:
            self.state["data"]["youtube_shorts_id"] = m.group(1)
            print(f"  [OK] Short uploaded: https://youtu.be/{m.group(1)}")
        else:
            fail(10, "UPLOAD_SHORTS_NO_VIDEO_ID", f"could not extract Shorts ID: {out[:200]}")

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

        # Verify removal
        if self.render_dir.exists():
            fail(11, "CLEANUP_RENDER_DIR_FAILED", f"render dir still exists after rmtree: {self.render_dir}")
        if state_path.exists():
            fail(11, "CLEANUP_STATE_FAILED", f"pipeline state file still exists: {state_path}")
        if not removed:
            print("  Nothing to clean up")
        else:
            print(f"  [OK] Removed {len(removed)} item(s)")


def main() -> None:
    parser = argparse.ArgumentParser(description="E2E Pipeline: .dem -> render -> thumbnail -> upload")
    parser.add_argument("player", help="Player nickname (e.g. w0nderful)")
    parser.add_argument("map", help="Map name (e.g. Anubis)")
    parser.add_argument("hltv_url", help="HLTV match URL")
    parser.add_argument("--steam-id", required=True, help="Steam64 ID")
    parser.add_argument(
        "--demo",
        default=None,
        help="Optional local .dem or .rar override (omit to download from HLTV URL)",
    )
    parser.add_argument("--tournament", required=True, help="Tournament name (e.g. IEM Atlanta 2026)")
    parser.add_argument("--step", type=int, default=1, choices=range(1, 12),
                        help="Start from step N (1=acquire..11=cleanup)")
    parser.add_argument("--until", type=int, default=None, choices=range(1, 11),
                        help="Stop after step N (default: 11). E.g. --until 9 stops before upload.")
    parser.add_argument("--resume-from-round", type=int, default=1,
                        help="Deprecated — filesystem-based resume is automatic. Keep for backward compat.")
    parser.add_argument("--force", action="store_true", help="Re-download HLTV archive even if present")
    parser.add_argument(
        "--force-avatar",
        action="store_true",
        help="Re-fetch player avatar even when valid cache exists",
    )
    parser.add_argument("--headless", action="store_true", help="CloakBrowser headless (default: visible)")
    parser.add_argument(
        "--profile-dir",
        default=None,
        help="CloakBrowser profile dir (default: .cloak-hltv-profile)",
    )
    parser.add_argument("--privacy", choices=["private", "unlisted", "public"], default="private")
    parser.add_argument("--batches", type=int, default=10,
                        help="Rounds per render batch (default: 10). 0 = all rounds in one session.")
    parser.add_argument("--publish-at", default=AUTO_PUBLISH_MODE,
                        help="Schedule YouTube publish: 'auto' = next future 16:30 Australia/Sydney, or explicit 'YYYY-MM-DD HH:MM'")
    parser.add_argument(
        "--publish-shorts-at",
        default=None,
        help="Schedule Shorts publish (defaults to --publish-at if omitted)",
    )
    parser.add_argument(
        "--timezone",
        default="Australia/Sydney",
        dest="publish_timezone",
        help="IANA timezone for --publish-at / --publish-shorts-at (default: Australia/Sydney)",
    )
    args = parser.parse_args()

    print(f"{'='*60}")
    print(f"  CS2Archive Pipeline")
    print(f"  Player: {args.player} | Map: {args.map}")
    print(f"  Demo:   {args.demo or '(acquire from HLTV)'}")
    print(f"  Step:   {args.step}" + (f" -> {args.until}" if args.until else " -> 11"))
    print(f"{'='*60}")

    Pipeline(args).run()


if __name__ == "__main__":
    main()
