"""Picker agent: pick parsing, id validation, and render-queue ordering."""

from __future__ import annotations

import json

from cs2archive.shorts.allstar_selector import order_by_pick
from cs2archive.shorts.picker_agent import MODELS, parse_pick

ANSWER = """## Ranking

1. aaa — donk, Mirage, 4K — fast and clean
2. bbb — ZywOo, Dust2, ace — eco

## My pick

aaa, because the buy was full and the kills were 1.2s apart.

```json
{"ranking": ["aaa", "bbb"], "pick": "aaa", "reason": "full buy, 1.2s span"}
```
"""


def test_parse_pick_reads_the_fenced_block():
    pick = parse_pick(ANSWER)
    assert pick == {"ranking": ["aaa", "bbb"], "pick": "aaa",
                    "reason": "full buy, 1.2s span"}


def test_parse_pick_drops_ids_the_agent_invented():
    pick = parse_pick(ANSWER, valid_ids={"aaa"})
    assert pick["ranking"] == ["aaa"]
    assert pick["pick"] == "aaa"


def test_parse_pick_falls_back_to_the_top_of_the_ranking():
    text = '```json\n{"ranking": ["bbb", "aaa"]}\n```'
    assert parse_pick(text)["pick"] == "bbb"


def test_parse_pick_takes_the_last_block_so_a_draft_cannot_win():
    text = ('```json\n{"ranking": ["aaa"], "pick": "aaa"}\n```\n'
            'On reflection:\n'
            '```json\n{"ranking": ["bbb"], "pick": "bbb"}\n```\n')
    assert parse_pick(text)["pick"] == "bbb"


def test_parse_pick_returns_none_without_a_usable_block():
    assert parse_pick("") is None
    assert parse_pick("no json here") is None
    assert parse_pick("```json\nnot json\n```") is None
    assert parse_pick('```json\n{"ranking": []}\n```') is None


def test_parse_pick_ignores_a_pick_outside_the_valid_set():
    pick = parse_pick('```json\n{"ranking": ["zzz"], "pick": "zzz"}\n```',
                      valid_ids={"aaa"})
    assert pick is None


def test_models_table_covers_the_bake_off():
    assert {"ultra", "super", "glm", "dsv4f"} <= set(MODELS)
    for provider, model, thinking in MODELS.values():
        assert provider and model and thinking


# ── render-queue ordering ────────────────────────────────────────────────
class _Cand:
    def __init__(self, key):
        self.key = key


def _ranked(*pairs):
    return [(_Cand(clip_id), {"clip_id": clip_id, "views": views})
            for clip_id, views in pairs]


def test_order_by_pick_puts_the_agents_order_first():
    ranked = _ranked(("aaa", 9000), ("bbb", 5000), ("ccc", 1000))
    ordered, report = order_by_pick(ranked, {"ranking": ["ccc", "aaa"], "pick": "ccc"})
    assert [clip["clip_id"] for _c, clip in ordered] == ["ccc", "aaa", "bbb"]
    assert report["applied"] is True
    assert report["matched"] == 2
    assert report["unmatched"] == 1
    assert report["headline"] == "ccc"


def test_order_by_pick_keeps_view_order_for_unranked_clips():
    ranked = _ranked(("aaa", 9000), ("bbb", 5000), ("ccc", 1000))
    ordered, _report = order_by_pick(ranked, {"ranking": ["bbb"], "pick": "bbb"})
    assert [clip["clip_id"] for _c, clip in ordered] == ["bbb", "aaa", "ccc"]


def test_order_by_pick_is_a_no_op_without_a_pick():
    ranked = _ranked(("aaa", 9000), ("bbb", 5000))
    ordered, report = order_by_pick(ranked, None)
    assert [clip["clip_id"] for _c, clip in ordered] == ["aaa", "bbb"]
    assert report["applied"] is False
    ordered, report = order_by_pick(ranked, {"ranking": []})
    assert report["applied"] is False


def test_order_by_pick_ignores_a_headline_that_is_not_in_the_pool():
    ranked = _ranked(("aaa", 9000), ("bbb", 5000))
    ordered, report = order_by_pick(ranked, {"ranking": ["bbb"], "pick": "zzz"})
    assert [clip["clip_id"] for _c, clip in ordered] == ["bbb", "aaa"]
    assert report["headline"] == "zzz"


def test_order_by_pick_round_trips_a_real_pick_payload():
    ranked = _ranked(("aaa", 9000), ("bbb", 5000))
    pick = parse_pick(ANSWER, valid_ids={"aaa", "bbb"})
    ordered, report = order_by_pick(ranked, pick)
    assert [clip["clip_id"] for _c, clip in ordered] == ["aaa", "bbb"]
    assert report["applied"] is True
    assert json.dumps(report)  # report stays serialisable for the ledger
