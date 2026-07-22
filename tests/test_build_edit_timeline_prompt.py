"""Prompt content for edit-timeline LLM batches."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from _pathsetup import ensure

ensure()

from highlights.build_edit_timeline import (  # noqa: E402
    _build_batch_prompt,
    _edit_timeline_few_shot_examples,
)


def _minimal_batch() -> dict:
    return {
        "min_round": 0,
        "max_round": 0,
        "kill_count": 0,
        "kills": [],
        "bomb_actions": [],
        "round_ends": [],
    }


def test_few_shot_examples_cover_warmup_handoff_and_round_split():
    text = _edit_timeline_few_shot_examples()
    assert "Example 1" in text and "Warmup" in text
    assert "Example 2" in text and "Omit segment" in text
    assert "Example 3" in text and "One round per segment" in text
    assert "76561198000000001" in text


def test_batch_prompt_includes_few_shot_and_real_batch_marker():
    action = {"map": "de_test"}
    players = {"76561198000000099": "TestPlayer"}
    prompt = _build_batch_prompt(_minimal_batch(), action, players)
    assert "FEW-SHOT EXAMPLES" in prompt
    assert "NOW EDIT THE REAL BATCH BELOW" in prompt
    assert "KILLS IN BATCH: 0" in prompt
