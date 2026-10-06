"""Stage label handling for POV descriptions/tags.

Two failure modes are pinned here:

1. ``extract_match_stage`` (scrapers/ratings.py) shares one HLTV node between the
   stage and the map-veto list. The old star regex terminated on a bare ``.``, so
   it stopped at the veto item's ``1.`` and captured
   ``"Swiss round 2 (teams with a 1-0 record) 1"`` — the dangling veto item
   number then reached the description and tags.
2. Only semi-finals / finals / grand finals are worth naming in the description.
   Everything earlier (Swiss, group, bracket, quarter-final) is dropped.
"""

from __future__ import annotations

from cs2archive.pov.generate_title import normalize_stage, show_stage
from scrapers.ratings import extract_match_stage

VETO_BLOB = (
    "* Swiss round 2 (teams with a 1-0 record) 1. Spirit removed Ancient "
    "2. PARIVISION removed Dust2 3. Spirit removed Mirage"
)


def _info_box(text: str) -> str:
    return f'<div class="match-info-box"><div class="text">{text}</div></div>'


# --- extractor -----------------------------------------------------------------


def test_veto_item_number_is_not_part_of_the_stage() -> None:
    stage = extract_match_stage(f"<span>{VETO_BLOB}</span>")

    assert stage == "Swiss round 2 (teams with a 1-0 record)"


def test_info_box_keeps_stage_and_drops_the_veto_list() -> None:
    stage = extract_match_stage(_info_box(VETO_BLOB))

    assert stage == "Swiss round 2 (teams with a 1-0 record)"


def test_star_bullet_is_stripped() -> None:
    assert extract_match_stage(_info_box("* Stage 2 semi-final")) == "Stage 2 semi-final"


def test_grand_final_sentence_keeps_only_the_label() -> None:
    stage = extract_match_stage(
        _info_box("* Grand final. Winner advances to the BLAST World Final"))

    assert stage == "Grand final"


def test_glued_veto_number_is_cut_from_an_info_box_dump() -> None:
    dump = (
        "Round of 16 1. Aurora removed Inferno 2. FURIA removed Anubis "
        "7. Mirage was left over Mirage FURIA 13 STATS ( 3 : 9 ; 10 : 0 ) "
        "Rewatch Demo download"
    )

    assert extract_match_stage(_info_box(dump)) == "Round of 16"


# --- description / tag gate ----------------------------------------------------


def test_grand_final_and_finals_are_shown() -> None:
    assert show_stage("Grand final") == "Grand final"
    assert show_stage("Final") == "Final"
    assert show_stage("Stage 3 final") == "Stage 3 final"
    assert show_stage("Stage 2 semi-final") == "Stage 2 semi-final"


def test_earlier_rounds_are_dropped() -> None:
    assert show_stage("Swiss round 2 (teams with a 1-0 record) 1") == ""
    assert show_stage("Swiss round 1 1") == ""
    assert show_stage("Quarter-final") == ""
    assert show_stage("Group B lower bracket round 1 1") == ""
    assert show_stage("Group A upper bracket quarter-final 1") == ""
    assert show_stage("3rd place decider") == ""
    assert show_stage("") == ""


def test_long_scrape_of_an_important_stage_falls_back_to_the_normalized_name() -> None:
    blob = "Grand final " + "x" * 80

    assert show_stage(blob) == "Grand Final"


def test_normalize_stage_classification_is_unchanged() -> None:
    assert normalize_stage("Grand final") == "Grand Final"
    assert normalize_stage("Stage 2 semi-final") == "Semi-Final"
    assert normalize_stage("Quarter-final") == "Quarter-Final"
    assert normalize_stage("Group Stage") == "Group Stage"
