"""Clip identity links and match groups, with explicit ambiguity.

A link is only *exact* when it is derived from an identifier the platform itself
exposes (Allstar clip id, or a clip id embedded in a caption). Everything else
is fuzzy: stored with its confidence, flagged ``ambiguous`` when more than one
foundation clip qualifies, and excluded from grouping/training.

Grouping rules (used by the experimental trainer):

* posts sharing a verified foundation clip id -> one group (cross-platform link)
* otherwise posts sharing a verified match id -> one group (same match)
* everything else -> its own group (no leakage through fuzzy guesses)
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from cs2archive.shorts.social.models import LinkRecord

CLIP_ID_RE = re.compile(r"\b([0-9a-f]{24})\b")
MAP_WORDS = ("inferno", "mirage", "dust", "ancient", "nuke", "overpass", "vertigo",
             "train", "anubis", "cache", "office", "italy")


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}


def build_foundation_index(foundation_dir: Path) -> dict[str, dict]:
    index: dict[str, dict] = {}
    clips_dir = Path(foundation_dir) / "clips"
    if not clips_dir.exists():
        return index
    for clip_dir in clips_dir.iterdir():
        if not clip_dir.is_dir():
            continue
        align = _load(clip_dir / "alignment.json")
        match = _load(clip_dir / "match.json")
        raw = _load(clip_dir / "raw_allstar.json")
        clip_id = str(align.get("clip_id") or raw.get("clip_id") or clip_dir.name)
        index[clip_id] = {
            "clip_id": clip_id,
            "match_id": str(align.get("match_id") or match.get("match_id") or "") or None,
            "map": match.get("map"),
            "player": align.get("player") or raw.get("player"),
            "round": align.get("round"),
            "quarantined": bool(align.get("quarantined")),
            "confidence": align.get("confidence"),
            "kinds": raw.get("kinds") or [],
            "clip_dir": str(clip_dir),
        }
    return index


def _map_in(text: str) -> str | None:
    low = text.lower()
    for name in MAP_WORDS:
        if name in low:
            return name
    return None


def link_posts(store, *, foundation_dir: Path, run_id: str) -> dict:
    # Rebuild all derived links: a changed matcher must not leave stale rows.
    removed = store.clear_links()
    index = build_foundation_index(foundation_dir)
    by_match: dict[str, list[str]] = {}
    for clip_id, meta in index.items():
        if meta.get("match_id"):
            by_match.setdefault(meta["match_id"], []).append(clip_id)
    stats = {"indexed_clips": len(index), "exact": 0, "caption_id": 0,
             "fuzzy": 0, "ambiguous": 0, "unlinked": 0,
             "stale_link_rows_removed": removed}
    quarantined_posts: set[tuple[str, str]] = set()

    for row in store.conn.execute("SELECT * FROM posts"):
        platform, post_id = row["platform"], row["post_id"]
        caption = str(row["caption"] or "")
        linked_any = False

        if platform == "allstar" and post_id in index:
            meta = index[post_id]
            store.upsert_link(LinkRecord(
                platform=platform, post_id=post_id, foundation_clip_id=post_id,
                match_id=meta.get("match_id"), method="allstar_clip_id",
                confidence=1.0, ambiguous=False,
                evidence={"match_id": meta.get("match_id"), "map": meta.get("map"),
                          "round": meta.get("round")},
                map_name=meta.get("map"), player=meta.get("player"),
                round_no=meta.get("round")), run_id=run_id)
            stats["exact"] += 1
            linked_any = True
            if meta.get("quarantined"):
                quarantined_posts.add((platform, post_id))

        if not linked_any:
            candidates = sorted({cid for cid in CLIP_ID_RE.findall(caption) if cid in index})
            if candidates:
                for clip_id in candidates:
                    meta = index[clip_id]
                    store.upsert_link(LinkRecord(
                        platform=platform, post_id=post_id, foundation_clip_id=clip_id,
                        match_id=meta.get("match_id"), method="clip_id_in_caption",
                        confidence=0.95, ambiguous=len(candidates) > 1,
                        evidence={"candidates": candidates},
                        map_name=meta.get("map"), player=meta.get("player"),
                        round_no=meta.get("round")), run_id=run_id)
                if len(candidates) > 1:
                    stats["ambiguous"] += 1
                else:
                    stats["caption_id"] += 1
                linked_any = True
                for clip_id in candidates:
                    if index[clip_id].get("quarantined"):
                        quarantined_posts.add((platform, post_id))

        if not linked_any:
            fuzzy = _fuzzy_candidates(row, index)
            if fuzzy:
                # Titles/player/map are not stable identifiers: always ambiguous.
                ambiguous = True
                for clip_id in fuzzy["clip_ids"]:
                    meta = index[clip_id]
                    store.upsert_link(LinkRecord(
                        platform=platform, post_id=post_id, foundation_clip_id=clip_id,
                        match_id=meta.get("match_id"), method=fuzzy["method"],
                        confidence=fuzzy["confidence"], ambiguous=ambiguous,
                        evidence={"reason": fuzzy["reason"],
                                  "candidates": fuzzy["clip_ids"]},
                        map_name=meta.get("map"), player=meta.get("player"),
                        round_no=meta.get("round")), run_id=run_id)
                stats["fuzzy"] += 1
                if ambiguous:
                    stats["ambiguous"] += 1
            else:
                stats["unlinked"] += 1

    for platform, post_id in quarantined_posts:
        store.add_quarantine(platform=platform, post_id=post_id,
                             reason="foundation_alignment_quarantined",
                             detail={"source": "alignment.json"}, run_id=run_id)
    stats["quarantined_links"] = len(quarantined_posts)
    return stats


def player_index(index: dict[str, dict]) -> dict[str, set[str]]:
    """lowercased foundation player name -> clip ids (longest names first)."""
    players: dict[str, set[str]] = {}
    for clip_id, meta in index.items():
        name = str(meta.get("player") or "").strip().lower()
        if len(name) >= 3:
            players.setdefault(name, set()).add(clip_id)
    return players


_KILL_WORD_RE = re.compile(r"\b([1-5])\s*k\b|\bace\b", re.I)


def _kill_hint(caption: str) -> int | None:
    if re.search(r"\bace\b", caption, re.I):
        return 5
    m = re.search(r"\b([1-5])\s*k\b", caption, re.I)
    return int(m.group(1)) if m else None


def _fuzzy_candidates(row, index: dict[str, dict]) -> dict | None:
    """Player + map (+ round/kill hint) from the caption or channel name.

    This is a *hypothesis*: player/map/round are not stable identifiers, so the
    link is always recorded ambiguous and never used for grouping or training.
    """
    caption = str(row["caption"] or "")
    creator = str(row["creator_name"] or "").lower().strip()
    map_hint = _map_in(caption)
    round_hint = None
    m = re.search(r"\bround\s*(\d{1,2})\b", caption, re.I)
    if m:
        round_hint = int(m.group(1))
    kill_hint = _kill_hint(caption)

    players = player_index(index)
    mentioned = sorted(
        (name for name in players
         if re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", caption, re.I)),
        key=lambda name: -len(name))
    player_hint = mentioned[0] if mentioned else (
        creator if creator in players else "")
    if not player_hint:
        return None
    if map_hint is None and round_hint is None and kill_hint is None:
        return None

    clip_ids = [
        clip_id for clip_id in sorted(players[player_hint])
        if (map_hint is None or str(index[clip_id].get("map") or "").lower() == map_hint)
        and (round_hint is None or index[clip_id].get("round") == round_hint)
    ]
    if not clip_ids:
        return None
    return {
        "clip_ids": clip_ids[:5],
        "method": "caption_player_map_kills_hypothesis",
        "confidence": 0.35,
        "reason": {"player": player_hint, "map": map_hint, "round": round_hint,
                   "kills_hint": kill_hint,
                   "player_from_caption": bool(mentioned),
                   "player_from_channel": (not mentioned and creator in players),
                   "note": ("hypothesis only: no stable clip id, so this link is "
                            "ambiguous and excluded from grouping and training")},
    }


def group_key(platform: str, post_id: str, links: list[dict]) -> tuple[str, str]:
    """Return ``(group_id, basis)`` for a post, refusing ambiguous links."""
    usable = [link for link in links
              if not link.get("ambiguous") and float(link.get("confidence") or 0) >= 0.8]
    clip_ids = sorted({link["foundation_clip_id"] for link in usable
                       if link.get("foundation_clip_id")})
    if len(clip_ids) == 1:
        return f"clip:{clip_ids[0]}", "foundation_clip"
    match_ids = sorted({str(link["match_id"]) for link in usable if link.get("match_id")})
    if len(match_ids) == 1:
        return f"match:{match_ids[0]}", "match"
    return f"post:{platform}:{post_id}", "post"


VERIFIED_LINK_MIN_CONFIDENCE = 0.8


def verified_links(links: list[dict]) -> list[dict]:
    """Links usable as identity evidence: not ambiguous, confidence >= 0.8."""
    return [link for link in links
            if not link.get("ambiguous")
            and float(link.get("confidence") or 0) >= VERIFIED_LINK_MIN_CONFIDENCE]


def assign_groups(posts: list[tuple[str, str]],
                  links_by_post: dict[tuple[str, str], list[dict]]) -> dict:
    """Connected components over verified clip *and* match identities.

    Two posts that share a match (via different clips) are in the same component,
    so a same-match pair cannot be split across train/test. Posts whose only
    links are ambiguous/fuzzy, or that have no links, get a private group and are
    reported as ``post_only`` (not verified/dedup-safe).
    """
    parent: dict[str, str] = {}

    def find(node: str) -> str:
        parent.setdefault(node, node)
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != root:
            parent[node], node = root, parent[node]
        return root

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    verified_posts: set[tuple[str, str]] = set()
    for platform, post_id in posts:
        node = f"post:{platform}:{post_id}"
        find(node)
        for link in verified_links(links_by_post.get((platform, post_id), [])):
            verified_posts.add((platform, post_id))
            clip_id = link.get("foundation_clip_id")
            if clip_id:
                union(node, f"clip:{clip_id}")
            match_id = link.get("match_id")
            if match_id:
                union(node, f"match:{match_id}")

    members: dict[str, list[str]] = {}
    for node in list(parent):
        members.setdefault(find(node), []).append(node)
    labels: dict[str, str] = {}
    for root, nodes in members.items():
        clip_nodes = sorted(n for n in nodes if n.startswith("clip:"))
        match_nodes = sorted(n for n in nodes if n.startswith("match:"))
        anchor = (clip_nodes or match_nodes or [root])[0]
        labels[root] = f"group:{anchor.split(':', 1)[1]}"

    out: dict[tuple[str, str], dict] = {}
    for platform, post_id in posts:
        root = find(f"post:{platform}:{post_id}")
        verified = (platform, post_id) in verified_posts
        out[(platform, post_id)] = {
            "group_id": labels[root],
            "basis": "verified" if verified else "post_only",
            "verified": verified,
        }
    return out


def identity_coverage(posts: list[tuple[str, str]],
                      links_by_post: dict[tuple[str, str], list[dict]]) -> dict:
    groups = assign_groups(posts, links_by_post)
    verified = sum(1 for value in groups.values() if value["verified"])
    multi = sum(1 for value in groups.values()
                if value["basis"] == "verified")
    return {
        "posts": len(posts),
        "verified_posts": verified,
        "unverified_posts": len(posts) - verified,
        "verified_share": (verified / len(posts)) if posts else 0.0,
        "verified_link_rows": multi,
        "groups": len({value["group_id"] for value in groups.values()}),
        "unverified_group_posts": len(posts) - verified,
    }
