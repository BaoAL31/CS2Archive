"""One Short per player per calendar day."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "upload"))

from shorts_player_day import meta_publish_date_local, player_blocked_slots, pov_nick_from_meta_path


def test_pov_nick_from_timeline(tmp_path: Path) -> None:
    (tmp_path / "short_timeline.json").write_text(
        json.dumps({"shorts": [{"pov_nick": "m0NESY"}]}), encoding="utf-8"
    )
    meta = tmp_path / "upload_meta_shorts.json"
    meta.write_text("{}", encoding="utf-8")
    assert pov_nick_from_meta_path(meta) == "m0NESY"


def test_blocks_both_slots_on_player_date(tmp_path: Path) -> None:
    a = tmp_path / "a"
    a.mkdir()
    (a / "short_timeline.json").write_text(
        json.dumps({"shorts": [{"pov_nick": "donk"}]}), encoding="utf-8"
    )
    (a / "upload_meta_shorts.json").write_text(
        json.dumps({"publish_at": "2026-09-02 12:00"}), encoding="utf-8"
    )
    b = tmp_path / "b"
    b.mkdir()
    (b / "short_timeline.json").write_text(
        json.dumps({"shorts": [{"pov_nick": "donk"}]}), encoding="utf-8"
    )
    exclude = b / "upload_meta_shorts.json"
    exclude.write_text(json.dumps({"publish_at": "auto"}), encoding="utf-8")

    blocked = player_blocked_slots(
        tmp_path, "donk", "Australia/Sydney", ["12:00", "18:00"],
        exclude_meta=exclude,
    )
    assert blocked == {("2026-09-02", "12:00"), ("2026-09-02", "18:00")}


def test_other_player_does_not_block() -> None:
    assert meta_publish_date_local({"publish_at": "auto"}, "Australia/Sydney") is None
    assert meta_publish_date_local(
        {"publish_at_utc": "2026-09-02T02:00:00.000Z"}, "Australia/Sydney"
    ) == "2026-09-02"
