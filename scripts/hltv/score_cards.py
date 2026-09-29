"""DEPRECATED (CR-01): this module moved to ``cs2archive.hltv.score_cards``.

Kept as a one-release-window compatibility shim so documented command lines, stale docs and
muscle memory keep working. Every invocation prints a deprecation notice on stderr.

Delete this file once the sunset check passes (see docs/reviews/council-log.md):
no ``DEPRECATED`` lines in listener logs and no remaining ``scripts/`` literals in code.
"""
from __future__ import annotations

import sys

sys.stderr.write(
    "DEPRECATED scripts/hltv/score_cards.py -> python -m cs2archive.hltv.score_cards\n"
)

_TARGET = "cs2archive.hltv.score_cards"

if __name__ == "__main__":
    import runpy

    runpy.run_module(_TARGET, run_name="__main__", alter_sys=True)
else:
    import importlib

    _module = importlib.import_module(_TARGET)
    for _name, _value in vars(_module).items():
        if not _name.startswith("_"):
            globals()[_name] = _value
