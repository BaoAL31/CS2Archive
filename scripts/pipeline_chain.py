"""
Start the next pipeline when the previous run reaches upload (state step >= 10).

Usage:
    python scripts/pipeline_chain.py --watch <run_id> --then <pipeline args...>
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = PROJECT_ROOT / ".pipeline"
PY = sys.executable


def wait_for_upload_step(run_id: str, poll_seconds: int = 30) -> None:
    path = STATE_DIR / f"{run_id}.json"
    print(f"[chain] Waiting for {run_id} to reach upload (step >= 10)...")
    while True:
        if path.exists():
            try:
                step = json.loads(path.read_text()).get("step", 1)
            except (json.JSONDecodeError, OSError):
                step = 1
            if step >= 10:
                print(f"[chain] {run_id} at step {step} — starting next pipeline")
                return
        time.sleep(poll_seconds)


def run_pipeline(argv: list[str]) -> subprocess.Popen:
    cmd = [PY, str(PROJECT_ROOT / "scripts" / "pipeline.py"), *argv]
    print(f"[chain] Launching: {' '.join(cmd)}")
    return subprocess.Popen(cmd, cwd=str(PROJECT_ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(description="Chain pipelines at upload step")
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

    wait_for_upload_step(args.watch, args.poll)
    proc = run_pipeline(args.then_args)
    if args.no_wait:
        return
    if proc.wait() != 0:
        sys.exit(proc.returncode)


if __name__ == "__main__":
    main()
