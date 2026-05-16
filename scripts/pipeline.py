"""
End-to-end pipeline: demo -> render -> thumbnail -> upload.
Resumable — tracks progress in a .pipeline_state.json per run.

Usage:
    python scripts/pipeline.py <rar_or_dem> <player> <map> <hltv_url> [--steam-id <id>] [--tournament "IEM Atlanta 2026"] [--step 1] [--privacy unlisted]
"""

from __future__ import annotations

import argparse
import asyncio
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
os.chdir(str(PROJECT_ROOT))

sys.path.insert(0, str(PROJECT_ROOT))

RENDER_DIR = PROJECT_ROOT / "demos" / "renders"
YOUTUBE_DIR = PROJECT_ROOT / "youtube"

STEPS = {
    1: "extract_rar",
    2: "ratings",
    3: "steam_id",
    4: "avatar",
    5: "analyze",
    6: "render",
    7: "concat",
    8: "thumbnail",
    9: "upload",
    10: "cleanup",
}


def load_state(run_id: str) -> dict:
    path = PROJECT_ROOT / f".pipeline_{run_id}.json"
    if path.exists():
        return json.loads(path.read_text())
    return {"step": 1, "data": {}}


def save_state(run_id: str, state: dict) -> None:
    (PROJECT_ROOT / f".pipeline_{run_id}.json").write_text(json.dumps(state, indent=2))


def run_id_from_name(name: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    return safe[:80].strip("_")


class Pipeline:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.run_id = run_id_from_name(f"{Path(args.demo).stem}_{args.player}_{args.map}")
        self.state = load_state(self.run_id)
        self.start_step = args.step
        self.rar_path: Path | None = None
        self.demo_path: Path | None = None
        self.demo_folder: Path | None = None
        self.steam_id: str = args.steam_id or ""
        self.output_dir: Path | None = None
        self.youtube_dir: Path | None = None

        src = Path(args.demo)
        if src.suffix.lower() == ".rar":
            self.rar_path = src
        elif src.suffix.lower() == ".dem":
            self.demo_path = src
        else:
            print(f"[ERROR] Unsupported file: {src.suffix} (use .rar or .dem)")
            sys.exit(1)

    def run(self) -> None:
        for step_num in range(self.start_step, max(STEPS.keys()) + 1):
            step_name = STEPS[step_num]
            print(f"\n{'='*60}")
            print(f"  Step {step_num}/{max(STEPS.keys())}: {step_name}")
            print(f"{'='*60}")

            try:
                getattr(self, f"step_{step_name}")()
                self.state["step"] = step_num + 1
                save_state(self.run_id, self.state)
            except StopIteration:
                break

        print(f"\nPipeline complete. youtube/{self.run_id}/")

    def step_extract_rar(self) -> None:
        if self.demo_path:
            print("  Demo already extracted, skipping")
            return
        if not self.rar_path:
            return

        import patoolib

        out = PROJECT_ROOT / "demos" / "hltv"
        out.mkdir(parents=True, exist_ok=True)
        patoolib.extract_archive(str(self.rar_path), outdir=str(out))
        files = sorted(out.glob("*.dem"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            print("[ERROR] No .dem files found in archive")
            sys.exit(1)

        target = [f for f in files if self.args.map.lower() in f.stem.lower()]
        if target:
            self.demo_path = target[0]
        else:
            self.demo_path = files[0]
            print(f"  No exact map match, using: {self.demo_path.name}")

        self.state["data"]["demo_path"] = str(self.demo_path)
        print(f"  Demo: {self.demo_path.name}")

        if self.args.steam_id:
            self.steam_id = self.args.steam_id

    def step_ratings(self) -> None:
        result = subprocess.run(
            [sys.executable, "main.py", "ratings", self.args.hltv_url],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            print(f"  [WARN] ratings returned {result.returncode}: {result.stderr[:200]}")
        else:
            print("  Ratings saved")

    def step_steam_id(self) -> None:
        if self.steam_id:
            print(f"  Steam ID provided: {self.steam_id}")
            return

        if not self.demo_path:
            print("[ERROR] No demo path for steam ID extraction")
            sys.exit(1)

        result = subprocess.run(
            [sys.executable, "scripts/extract_steamids.py", str(self.demo_path)],
            capture_output=True, text=True, timeout=120,
        )
        out = (result.stdout or "") + (result.stderr or "")
        for line in out.splitlines():
            if self.args.player.lower() in line.lower():
                parts = line.split()
                for p in parts:
                    if p.isdigit() and len(p) == 17:
                        self.steam_id = p
                        break
                if self.steam_id:
                    break

        if not self.steam_id:
            print(f"[ERROR] Could not find steam ID for '{self.args.player}'")
            sys.exit(1)

        result = subprocess.run(
            [sys.executable, "main.py", "player", "add", self.args.player, "--steam", self.steam_id],
            capture_output=True, text=True, timeout=15,
        )
        self.state["data"]["steam_id"] = self.steam_id
        print(f"  Steam ID: {self.steam_id}")

    def step_avatar(self) -> None:
        from scrapers.player_images import get_player_avatars

        avatars = asyncio.run(get_player_avatars(self.args.hltv_url))
        if self.args.player.lower() in avatars:
            print(f"  Avatar: {avatars[self.args.player.lower()]}")
        else:
            print(f"  [WARN] No avatar for {self.args.player}")

    def step_analyze(self) -> None:
        if not self.demo_path:
            print("[ERROR] No demo path for analysis")
            sys.exit(1)

        from config import settings

        result = subprocess.run(
            ["csdm", "analyze", str(self.demo_path)],
            capture_output=True, text=True, timeout=300,
        )
        out = (result.stdout or "") + (result.stderr or "")
        if "already in database" in out:
            print("  Already analyzed")
        elif result.returncode == 0:
            print("  Analysis done")
        else:
            print(f"  [WARN] Analyze returned {result.returncode}")

    def step_render(self) -> None:
        if not self.demo_path:
            print("[ERROR] No demo path for rendering")
            sys.exit(1)
        if not self.steam_id:
            print("[ERROR] No steam ID for rendering")
            sys.exit(1)

        result = subprocess.run(
            [sys.executable, "scripts/render_pov.py", str(self.demo_path), self.steam_id],
            timeout=7200,
        )
        if result.returncode != 0 and result.returncode != 1:
            print(f"  [WARN] render returned exit code {result.returncode}")

        stem = self.demo_path.stem
        self.output_dir = RENDER_DIR / f"pov-{stem}"
        if self.output_dir.exists():
            self.state["data"]["render_dir"] = str(self.output_dir)
            print(f"  Render output: {self.output_dir}")

    def step_concat(self) -> None:
        render_dir = self.state["data"].get("render_dir") or str(
            RENDER_DIR / f"pov-{self.demo_path.stem}"
        )
        render_path = Path(render_dir)
        if not render_path.exists():
            print(f"[ERROR] Render dir not found: {render_path}")
            sys.exit(1)

        combined = render_path / "combined.mp4"
        if combined.exists():
            print("  combined.mp4 already exists, skipping")
        else:
            result = subprocess.run(
                [sys.executable, "scripts/concat_rounds.py", str(render_path)],
                capture_output=True, text=True, timeout=600,
            )
            print((result.stdout or "") + (result.stderr or ""))

        self.youtube_dir = YOUTUBE_DIR / self.run_id
        self.youtube_dir.mkdir(parents=True, exist_ok=True)

        if combined.exists():
            dst = self.youtube_dir / "video.mp4"
            shutil.copy2(str(combined), str(dst))
            print(f"  Copied video.mp4 ({dst.stat().st_size / 1e9:.1f} GB)")
        self.state["data"]["youtube_dir"] = str(self.youtube_dir)

    def step_thumbnail(self) -> None:
        if not self.youtube_dir:
            self.youtube_dir = YOUTUBE_DIR / self.run_id
            self.youtube_dir.mkdir(parents=True, exist_ok=True)

        thumb = self.youtube_dir / "thumbnail.png"
        if thumb.exists():
            print("  thumbnail.png already exists, skipping")
            return

        demo = self.state["data"].get("demo_path") or str(self.demo_path)
        extra = []
        if self.args.tournament:
            extra += ["--tournament", self.args.tournament]

        cmd = [
            sys.executable, "-m", "thumbnail",
            self.args.hltv_url,
            "--player", self.args.player,
            "--map", self.args.map,
            "--demo", demo,
            "--steam-id", self.steam_id,
        ] + extra

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        print((result.stdout or "") + (result.stderr or ""))

    def step_upload(self) -> None:
        if not self.youtube_dir:
            self.youtube_dir = YOUTUBE_DIR / self.run_id

        video = self.youtube_dir / "video.mp4"
        thumb = self.youtube_dir / "thumbnail.png"

        if not video.exists():
            print(f"[ERROR] video.mp4 not found: {video}")
            sys.exit(1)

        title = f"{self.args.player} | NAVI vs Vitality | {self.args.map} | {self.args.tournament or 'IEM Atlanta 2026'}"

        cmd = [
            sys.executable, "scripts/upload_youtube.py",
            str(video),
            "--title", title,
            "--privacy", self.args.privacy,
        ]
        if thumb.exists():
            cmd += ["--thumbnail", str(thumb)]

        result = subprocess.run(cmd, capture_output=False, text=True, timeout=7200)
        if result.stdout:
            for line in result.stdout.splitlines():
                print(line)

    def step_cleanup(self) -> None:
        render_dir = self.state["data"].get("render_dir")
        if render_dir and Path(render_dir).exists():
            shutil.rmtree(render_dir)
            print(f"  Removed: {render_dir}")
        else:
            print("  No render dir to clean")

        state_path = PROJECT_ROOT / f".pipeline_{self.run_id}.json"
        if state_path.exists():
            state_path.unlink()
            print("  Pipeline state cleaned up")


def main() -> None:
    parser = argparse.ArgumentParser(description="E2E Pipeline: demo -> render -> upload")
    parser.add_argument("demo", help="Path to .rar or .dem file")
    parser.add_argument("player", help="Player nickname (e.g. w0nderful)")
    parser.add_argument("map", help="Map name (e.g. Anubis)")
    parser.add_argument("hltv_url", help="HLTV match URL")
    parser.add_argument("--steam-id", help="Steam64 ID (auto-extracted if omitted)")
    parser.add_argument("--tournament", default="", help="Tournament name (e.g. IEM Atlanta 2026)")
    parser.add_argument("--step", type=int, default=1, help="Start from step N  (1=extract, 2=ratings, 3=steam_id, 4=avatar, 5=analyze, 6=render, 7=concat, 8=thumbnail, 9=upload, 10=cleanup)")
    parser.add_argument("--privacy", choices=["private", "unlisted", "public"], default="unlisted")
    args = parser.parse_args()

    print(f"{'='*60}")
    print(f"  CS2Archive Pipeline")
    print(f"  Player: {args.player} | Map: {args.map}")
    print(f"  Starting at step {args.step}")
    print(f"{'='*60}")

    pipeline = Pipeline(args)
    pipeline.run()

    url_file = YOUTUBE_DIR / pipeline.run_id / "url.txt"
    if url_file.exists():
        print(f"  URL: {url_file.read_text().strip()}")


if __name__ == "__main__":
    main()
