"""Ensure CS2Archive script subpackages are importable.

Call ``ensure()`` at the top of any script under ``scripts/<bucket>/``.
Adds project root + ``scripts/`` + each bucket dir to ``sys.path``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPTS_DIR.parent
_BUCKETS = ("pov", "overlay", "faceit", "highlights", "upload", "hf", "misc")


def ensure() -> Path:
    """Insert import paths; return project root."""
    roots = [_PROJECT_ROOT, _SCRIPTS_DIR]
    roots.extend(_SCRIPTS_DIR / b for b in _BUCKETS)
    for p in reversed(roots):
        s = str(p)
        if s not in sys.path:
            sys.path.insert(0, s)
    return _PROJECT_ROOT


PROJECT_ROOT = _PROJECT_ROOT
SCRIPTS_DIR = _SCRIPTS_DIR
