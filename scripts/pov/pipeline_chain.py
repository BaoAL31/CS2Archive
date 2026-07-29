"""
Start the next pipeline when the previous run reaches thumbnail (step >= 6).

Since uploading is handled separately by upload_pending.py, the chain triggers
at step 6 (thumbnail/upload-ready). The pipeline writes upload_meta.json with
upload_status="pending" and stops; the actual YouTube upload runs separately
and can overlap with the next POV's acquire->render.

Usage:
    python scripts/pov/pipeline_chain.py --watch <run_id> -- <pipeline args...>
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = PROJECT_ROOT / ".pipeline"
PY = sys.executable


def wait_for_thumbnail(run_id: str, poll_seconds: int = 30) -> None:
    path = STATE_DIR / f"{run_id}.json"
    print(f"[chain] Waiting for {run_id} to reach thumbnail (step >= 6)...")
    while True:
        if path.exists():
            try:
                step = json.loads(path.read_text()).get("step", 1)
            except (json.JSONDecodeError, OSError):
                step = 1
            if step >= 6:
                print(f"[chain] {run_id} at step {step} -- thumbnail/upload-ready")
                return
        time.sleep(poll_seconds)


def run_pipeline(argv: list[str]) -> subprocess.Popen:
    cmd = [PY, str(PROJECT_ROOT / "scripts" / "pov" / "pipeline.py"), *argv]
    print(f"[chain] Launching: {' '.join(cmd)}")
    return subprocess.Popen(cmd, cwd=str(PROJECT_ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Chain pipelines at thumbnail step -- start the next POV "
                    "when the previous reaches step >= 6 (thumbnail/upload-ready). "
                    "Upload is handled separately by upload_pending.py.")
    parser.add_argument("--watch", required=True, help="run_id to watch (.pipeline/<run_id>.json)")
    parser.add_argument("--poll", type=int, default=30, help="Poll interval seconds")
    parser.add_argument("then_args", nargs=argparse.REMAINDER, help="Args for next pipeline.py run")
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Start next pipeline and exit (do not wait for it to finish)",
    )
    args = parser.parse_args()

    if args.then_args and args.then_args[0] == "--":
        args.then_args = args.then_args[1:]
    if not args.then_args:
        parser.error("Provide pipeline args after --watch <run_id> --")

    wait_for_thumbnail(args.watch, args.poll)
    proc = run_pipeline(args.then_args)
    if args.no_wait:
        return
    if proc.wait() != 0:
        sys.exit(proc.returncode)


if __name__ == "__main__":
    main()