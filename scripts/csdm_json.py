"""One `csdm json` helper with challengermode retry."""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from config import settings


def csdm_json(demo: Path, *, timeout: int = 300) -> dict:
    """Parse a demo via ``csdm json``. Retries with --source challengermode."""
    demo = Path(demo)
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [settings.csdm_cmd, "json", str(demo), "--output-folder", tmp]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if "unknown demo source" in (r.stderr or "").lower() + (r.stdout or "").lower():
            cmd = cmd + ["--source", "challengermode"]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip()[-400:]
            raise RuntimeError(f"csdm json failed for {demo.name}: {err}")
        files = list(Path(tmp).glob("*.json"))
        if not files:
            raise RuntimeError(f"csdm json wrote no JSON for {demo.name}")
        return json.loads(files[0].read_text(encoding="utf-8"))
