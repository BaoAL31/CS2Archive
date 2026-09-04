"""Keep pytest from mutating the real Steam / CSDM install."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


@pytest.fixture(autouse=True)
def _no_steam_hlae_preflight(monkeypatch):
    import hook_aware

    monkeypatch.setattr(hook_aware, "prepare_steam_hlae", lambda **k: None)
