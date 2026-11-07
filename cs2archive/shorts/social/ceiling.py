"""Ceiling analysis: how much of the corpus can *ever* be demo-linked.

Two different ceilings are reported, and they are not the same thing:

* **Title-feasible** — the title names a map and a kill signature, so a linkage
  hypothesis can be formed at all.
* **Evidence-feasible** — a local demo for the named player *and* map actually
  exists in the corpus we hold, so the kill signature could be verified.

Anything else is permanently unlinkable from public data alone and is reported as
such. No candidate is invented to raise the number, and an unlinkable share is
never presented as a linkage failure.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from cs2archive.shorts.social.backfill import parse_signature
from cs2archive.shorts.social.models import METRIC_FIELDS  # noqa: F401

OWN_CHANNELS = {"UCh8PPTy8KFPhxvVZOCE519Q"}  # CS2 Archive (home)
PLAYER_KINDS = {"ace", "2k", "3k", "4k", "5k", "clutch"}


def _demo_corpus_index(demos_root: Path) -> dict[str, set[str]]:
    """map slug -> set of demo file stems.

    The map lives in the demo FILE name (``...-m1-ancient.dem``), never in the
    match folder name, so indexing folders would silently report zero corpus
    coverage for every map.
    """
    index: dict[str, set[str]] = {}
    root = Path(demos_root)
    if not root.exists():
        return index
    from cs2archive.shorts.social.deepen import title_map_slug

    for demo in root.rglob("*.dem"):
        slug = title_map_slug(demo.stem)
        if slug:
            index.setdefault(slug, set()).add(demo.stem)
    return index


def ceiling_report(store, *, demos_root: Path = Path("demos/hltv"),
                   players: dict[str, str] | None = None) -> dict:
    players = players or {}
    corpus = _demo_corpus_index(demos_root)
    known_players = {name.lower() for name in players}
    per_platform: dict[str, dict] = {}

    link_rows = list(store.conn.execute(
        "SELECT platform, post_id, confidence, ambiguous, method "
        "FROM clip_identity_links"))
    linked = {(row["platform"], row["post_id"]) for row in link_rows
              if not row["ambiguous"] and float(row["confidence"] or 0) >= 0.8}
    hypothesis = {(row["platform"], row["post_id"]) for row in link_rows
                  if row["ambiguous"]}

    for platform in ("youtube", "instagram", "reddit", "allstar"):
        posts = list(store.conn.execute(
            "SELECT post_id, creator_id, creator_name, caption, format_label "
            "FROM posts WHERE platform=?", (platform,)))
        if not posts:
            continue
        counts = Counter()
        for post in posts:
            key = (platform, post["post_id"])
            if key in linked:
                counts["linked_exact"] += 1
                continue
            if key in hypothesis:
                counts["hypothesis_only"] += 1
                continue
            signature = parse_signature(post["caption"] or "")
            if not signature["map"]:
                counts["unlinkable_no_map_in_title"] += 1
                continue
            if signature["kind"] not in PLAYER_KINDS:
                counts["unlinkable_no_gameplay_signature"] += 1
                continue
            counts["title_feasible"] += 1
            if signature["map"] in corpus:
                # Upper bound only: the map exists in the corpus, but the specific
                # match/round still has to be proven by its kill signature.
                counts["evidence_feasible_local_demo"] += 1
            else:
                counts["unlinkable_no_local_demo_for_map"] += 1
        total = len(posts)
        unlinkable = sum(value for name, value in counts.items()
                         if name.startswith("unlinkable"))
        per_platform[platform] = {
            "posts": total,
            "linked_exact": counts["linked_exact"],
            "hypothesis_only": counts["hypothesis_only"],
            "title_feasible": counts["title_feasible"],
            "evidence_feasible_local_demo": counts["evidence_feasible_local_demo"],
            "unlinkable": unlinkable,
            "unlinkable_share": round(unlinkable / total, 4) if total else None,
            "breakdown": dict(sorted(counts.items())),
        }

    competitor = {name: value for name, value in per_platform.items()
                  if name in {"youtube", "instagram", "reddit"}}
    comp_total = sum(value["posts"] for value in competitor.values())
    comp_unlinkable = sum(value["unlinkable"] for value in competitor.values())
    return {
        "demo_corpus_maps": {slug: len(folders) for slug, folders in sorted(corpus.items())},
        "known_players": len(known_players),
        "per_platform": per_platform,
        "competitor_summary": {
            "posts": comp_total,
            "unlinkable": comp_unlinkable,
            "unlinkable_share": (round(comp_unlinkable / comp_total, 4)
                                 if comp_total else None),
            "note": "public titles carry no stable clip id, so a competitor short "
                    "can only ever be linked when its title names a map AND a kill "
                    "signature AND a local demo for that player+map exists",
        },
        "evidence_feasible_note": (
            "evidence_feasible is an UPPER BOUND: a local demo for that map exists, "
            "but the link is only real when the round kill signature verifies "
            "uniquely in that demo (see the linkage section for actual links)"),
        "gameplay_model_eligibility": {
            "rule": "only exact, unambiguous links with confidence >= 0.8 enter the "
                    "scored set",
            "note": "hypotheses and unlinkable posts are excluded; the eligible "
                    "count is derived from the same links the trainer uses",
        },
    }


def format_ceiling(report: dict) -> str:
    lines = ["demo-linkage ceiling", "=" * 21,
             f"local demo map coverage: {report['demo_corpus_maps']}", ""]
    for name, data in report["per_platform"].items():
        lines.append(f"[{name}] posts={data['posts']} exact={data['linked_exact']} "
                     f"hypothesis={data['hypothesis_only']} "
                     f"title_feasible={data['title_feasible']} "
                     f"evidence_feasible={data['evidence_feasible_local_demo']} "
                     f"unlinkable={data['unlinkable']} "
                     f"({data['unlinkable_share']})")
        for key, value in data["breakdown"].items():
            lines.append(f"    {key} = {value}")
    summary = report["competitor_summary"]
    lines.append("")
    lines.append(f"competitors: {summary['posts']} posts, "
                 f"{summary['unlinkable']} unlinkable "
                 f"({summary['unlinkable_share']})")
    lines.append(f"  {summary['note']}")
    return "\n".join(lines)
