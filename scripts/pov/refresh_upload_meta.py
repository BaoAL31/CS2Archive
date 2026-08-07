"""Refresh upload_meta.json for an existing youtube dir via the pipeline's
own _write_upload_meta (the exact code path a real run uses at step 6).

Drives the pipeline title logic directly against an explicit target dir —
a normal `--step 6` resume would derive run_id with the current map-included
scheme and create a NEW folder, so we pass the existing folder explicitly.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _pathsetup import ensure  # noqa: E402

ensure()

from scripts.pov.pipeline import Pipeline  # noqa: E402


class Args:
    pass


args = Args()
args.backlog = sys.argv[1]          # backlog card path
args.step = 6
args.until = 6
args.no_cleanup = True
args.dual_upload = True
args.overlay_only = True

p = Pipeline(args)
target = Path(sys.argv[2])          # existing youtube/{run_id}_overlay dir
if not (target / "video.mp4").exists():
    print(f"[ERR] no video.mp4 in {target}")
    sys.exit(1)

p._write_upload_meta(target, variant="overlay", step_num=6)
print(f"[OK] upload_meta.json refreshed at {target / 'upload_meta.json'}")
