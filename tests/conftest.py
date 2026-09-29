"""Keep pytest from mutating the real Steam / CSDM install."""

from __future__ import annotations

import pytest

# CR-16 (expires 2026-11-15): tests/test_overlay_extraction.py is a CLI script, not a test module —
# its only two functions are named test_* but take (demo, steam_id[, round_num]) and are invoked
# from that file's own main() at lines 223-224. pytest collected them and reported two permanent
# "fixture not found" ERRORS. Suppressing collection is the root-cause fix; renaming them to
# check_* (and updating the two call sites) belongs to CR-16.
#
# This must be a conftest variable, NOT a [tool.pytest.ini_options] entry: pytest 9.1.1 warns
# "Unknown config option: collect_ignore_glob" and silently ignores the ini form.
collect_ignore_glob = ["test_overlay_extraction.py"]



@pytest.fixture(autouse=True)
def _no_steam_hlae_preflight(monkeypatch):
    import cs2archive.render.hook_aware as hook_aware

    monkeypatch.setattr(hook_aware, "prepare_steam_hlae", lambda **k: None)
