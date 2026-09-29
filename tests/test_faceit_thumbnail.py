from __future__ import annotations

import sys
from pathlib import Path


from cs2archive.faceit.faceit_thumbnail import style01_sub  # noqa: E402


def test_sub_is_with_line_for_single_teammate():
    assert style01_sub(["jL"]) == "w/ jL"


def test_sub_empty_without_teammate():
    assert style01_sub([]) == ""
    assert style01_sub(["jL", "Magixx"]) == ""
