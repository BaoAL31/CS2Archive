#!/usr/bin/env python3
"""
Test script to render a single util throw using CS2UtilArchive's render_utils,
outputting the result into CS2Archive/renders_test/.
Run from CS2UtilArchive must be a sibling directory (../CS2UtilArchive) and the conda
 environment `cs2archive` must contain pandas.
"""

import os
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------
# Configuration – adjust if needed
# ---------------------------------------------------------------
CS2ARCHIVE_ROOT = Path(__file__).resolve().parents[1]   # D:/Projects/CS2Archive
CS2UTIL_ROOT    = CS2ARCHIVE_ROOT.parent / "CS2UtilArchive"

# Demo & manifest (Niko smoke on Inferno, IEM Cologne 2026)
MANIFEST = CS2UTIL_ROOT / "renders" / "iem_cologne_major_2026" / "top_inferno_smokes" / "render_manifest.json"
DATA_DIR = CS2UTIL_ROOT / "results" / "iem_cologne_major_2026"
DEMO_ID  = "2395002-furia-vs-falcons-m3-inferno"
UTIL_ID  = "de_inferno:smoke:CT:256_1472_128"

# Output goes into CS2Archive/renders_test
OUTPUT_ROOT = CS2ARCHIVE_ROOT / "renders_test"

# Conda python that has pandas
CONDA_PY = Path(r"C:\Users\jembo\anaconda3\envs\cs2archive\python.exe")

# ---------------------------------------------------------------
def main() -> None:
    if not CONDA_PY.is_file():
        sys.exit(f"[ERROR] conda python not found at {CONDA_PY}")

    if not MANIFEST.is_file():
        sys.exit(f"[ERROR] manifest not found: {MANIFEST}")

    # Build command
    cmd = [
        str(CONDA_PY),
        "-m", "scripts.render_utils",          # run render_utils as module
        "--manifest", str(MANIFEST),
        "--data-dir", str(DATA_DIR),
        "--only-demo", DEMO_ID,
        "--only-util-id", UTIL_ID,
        "--cameras", "flight,detonate",       # flight + orbit for smoke
        "--output-root", str(OUTPUT_ROOT),
    ]

    env = os.environ.copy()
    # Ensure CS2UtilArchive is importable
    env["PYTHONPATH"] = str(CS2UTIL_ROOT)

    print(f"[INFO] Running: {' '.join(cmd)}")
    print(f"[INFO] PYTHONPATH={env['PYTHONPATH']}")
    print(f"[INFO] Output will be under {OUTPUT_ROOT}")

    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)

    if result.returncode != 0:
        sys.exit(f"[ERROR] render_utils exited with {result.returncode}")

    print(f"[OK] Util render finished – check {OUTPUT_ROOT}")

if __name__ == "__main__":
    main()