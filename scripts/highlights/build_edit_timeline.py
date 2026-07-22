"""Build Edit Timeline from Action Timeline using LLM (FACEIT only).

Reads Action Timeline, splits into round batches, prompts LLM per batch,
concatenates results, validates output, writes edit_timeline.json.

Post-LLM shaping lives in ``_fix_edit_timeline`` (anchoring, POV split, buy/warmup,
post-streak solo drops). Golden batch-1 reference: ``fixtures/cache_batch1_goal_segments.json``
(regression: ``tests/test_build_edit_timeline_batch1.py``).

Usage:
    python scripts/highlights/build_edit_timeline.py demos/faceit/<demo>.dem
    python scripts/highlights/build_edit_timeline.py --action-timeline renders/hl-<stem>/action_timeline.json
    python scripts/highlights/build_edit_timeline.py --fix-only --action-timeline ... --edit-timeline ...
    python scripts/highlights/build_edit_timeline.py demos/faceit/<demo>.dem --batch-size 3
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _pathsetup import ensure

PROJECT_ROOT = ensure()

# Load .env for OPENROUTER_API_KEY
from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from scripts.faceit.faceit_names import known_pro_steam_ids  # noqa: E402

try:
    import openai
except ImportError:
    openai = None


DEFAULT_MODEL = "mimo-v2.5-free"
FALLBACK_MODEL = "deepseek-v4-flash-free"
ZEN_BASE_URL = "https://opencode.ai/zen/v1"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_FALLBACK_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"


def _is_faceit_demo(path: Path) -> bool:
    try:
        path.resolve().relative_to((PROJECT_ROOT / "demos" / "faceit").resolve())
        return True
    except ValueError:
        return "demos/faceit" in str(path).replace("\\", "/")


def _highlights_run_dir(demo_path: Path) -> Path:
    return PROJECT_ROOT / "renders" / f"hl-{demo_path.stem}"


def _load_action_timeline(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not all(k in data for k in ("demo_path", "map", "source", "kill_count", "kills")):
        raise ValueError(f"Invalid action_timeline.json: missing required fields")
    if "round_starts" not in data or not data["round_starts"]:
        raise ValueError(
            f"action_timeline.json missing 'round_starts'. Re-run build_action_timeline.py "
            f"to regenerate ({path})."
        )
    return data


def _extract_players_from_action_timeline(at: dict) -> dict:
    """Extract unique players (steam_id -> name) from action timeline."""
    players = {}
    for k in at["kills"]:
        if k["attacker_steam_id"]:
            players[k["attacker_steam_id"]] = k["attacker"]
        if k["victim_steam_id"]:
            players[k["victim_steam_id"]] = k["victim"]
    for b in at.get("bomb_actions", []):
        if b["player_steam_id"]:
            players[b["player_steam_id"]] = b["player"]
    return players


def _get_pro_sids() -> set[str]:
    return set(known_pro_steam_ids().keys())


# ──────────────────────────────────────────────────────────────────────
# Round batching
# ──────────────────────────────────────────────────────────────────────

def _split_into_round_batches(action_timeline: dict, batch_size: int) -> list[dict]:
    """Split action timeline into per-batch sub-timelines by round.

    Each batch contains only the kills and bombs from its round range,
    with *local* 0-based kill indices (remapped after LLM returns).
    """
    kills = action_timeline["kills"]
    bombs = action_timeline.get("bomb_actions", [])
    round_starts = action_timeline.get("round_starts", [])
    round_ends = action_timeline.get("round_ends", [])

    # Determine round range
    all_rounds = sorted(set(k["round"] for k in kills) | set(b["round"] for b in bombs))
    if not all_rounds:
        return []

    batches = []
    for batch_start in range(0, len(all_rounds), batch_size):
        batch_rounds = all_rounds[batch_start:batch_start + batch_size]
        min_r, max_r = batch_rounds[0], batch_rounds[-1]

        # Global kill indices that fall in this batch's rounds
        batch_kill_global = [i for i, k in enumerate(kills) if k["round"] in batch_rounds]

        # Build local kills with local 0-based indices
        local_kills = []
        for local_i, global_i in enumerate(batch_kill_global):
            k = kills[global_i]
            local_kills.append({**k, "_local_index": local_i, "_global_index": global_i})

        batch_bombs = [b for b in bombs if b["round"] in batch_rounds]
        batch_round_starts = [r for r in round_starts if r["round"] in batch_rounds]
        batch_round_ends = [r for r in round_ends if r["round"] in batch_rounds]

        batches.append({
            "rounds": batch_rounds,
            "min_round": min_r,
            "max_round": max_r,
            "kills": local_kills,
            "kill_count": len(local_kills),
            "bomb_actions": batch_bombs,
            "round_starts": batch_round_starts,
            "round_ends": batch_round_ends,
            "global_kill_offset": batch_kill_global[0] if batch_kill_global else 0,
            "global_kill_indices": batch_kill_global,
        })

    return batches


# ──────────────────────────────────────────────────────────────────────
# Prompt building (per batch)
# ──────────────────────────────────────────────────────────────────────

# Synthetic steam IDs for few-shot examples only (not real players).
_EX_ALPHA = "76561198000000001"
_EX_BRAVO = "76561198000000002"


def _edit_timeline_few_shot_examples() -> str:
    """Compact synthetic I/O pairs (general patterns, not tied to any real match)."""
    ex1 = json.dumps(
        {
            "segments": [
                {
                    "start_tick": 128,
                    "end_tick": 550,
                    "pov_steam_id": _EX_ALPHA,
                    "segment_type": "multi_kill",
                    "kill_indices": [0, 1, 2],
                    "rationale": "Warmup round: one segment from round_start; Alpha POV (most kills in r0).",
                }
            ]
        },
        separators=(",", ":"),
    )
    ex2 = json.dumps(
        {
            "segments": [
                {
                    "start_tick": 11744,
                    "end_tick": 12320,
                    "pov_steam_id": _EX_ALPHA,
                    "segment_type": "multi_kill",
                    "kill_indices": [0, 1],
                    "rationale": "Alpha quick 2k; POV follows attacker.",
                },
                {
                    "start_tick": 12260,
                    "end_tick": 12500,
                    "pov_steam_id": _EX_BRAVO,
                    "segment_type": "default",
                    "kill_indices": [3],
                    "rationale": "Handoff to Bravo; omit low-story trade at local index 2 between streak and entry.",
                },
            ]
        },
        separators=(",", ":"),
    )
    ex3_good = json.dumps(
        {
            "segments": [
                {
                    "start_tick": 11744,
                    "end_tick": 12200,
                    "pov_steam_id": _EX_ALPHA,
                    "segment_type": "entry",
                    "kill_indices": [0],
                    "rationale": "Round 1 only.",
                },
                {
                    "start_tick": 20864,
                    "end_tick": 21300,
                    "pov_steam_id": _EX_BRAVO,
                    "segment_type": "entry",
                    "kill_indices": [1],
                    "rationale": "Round 2 separate segment; never mix rounds.",
                },
            ]
        },
        separators=(",", ":"),
    )
    return f"""
FEW-SHOT EXAMPLES (synthetic mini-batches; use LOCAL kill indices; 64 tick/s):

Example 1 — Warmup / knife (round 0): round_start r0 t128; next round r1 t5000.
  KILLS:
  [0] r0 t200 Alpha(PRO)>bot1
  [1] r0 t280 Alpha(PRO)>bot2
  [2] r0 t350 Bravo>bot3
  Expected JSON:
  {ex1}

Example 2 — Live round 1: round_start r1 t10000, round_freeze_end r1 t11536; next round r2 t20000.
  KILLS:
  [0] r1 t12000 Alpha(PRO)>x
  [1] r1 t12120 Alpha(PRO)>y
  [2] r1 t12180 Charlie>z
  [3] r1 t12300 Bravo(PRO)>w
  Omit segment for [2] (trade between highlights). Overlapping tick ranges for Alpha then Bravo are OK.
  Expected JSON:
  {ex2}

Example 3 — One round per segment (do NOT put r1 and r2 kills in one segment):
  KILLS:
  [0] r1 t12000 Alpha(PRO)>a
  [1] r2 t21000 Bravo(PRO)>b
  Expected JSON:
  {ex3_good}
"""


def _build_batch_prompt(batch: dict, action_timeline: dict, players: dict) -> str:
    pro_sids = _get_pro_sids()
    pro_marks = {sid: " (PRO)" for sid in pro_sids if sid in players}

    kills_summary = []
    for k in batch["kills"]:
        li = k["_local_index"]
        atk_pro = " (PRO)" if k["attacker_steam_id"] in pro_sids else ""
        vic_pro = " (PRO)" if k["victim_steam_id"] in pro_sids else ""
        kills_summary.append(
            f"[{li}] r{k['round']} t{k['tick']} {k['attacker']}{atk_pro}>{k['victim']}{vic_pro}({k['weapon']})"
        )

    bomb_summary = []
    for b in batch["bomb_actions"]:
        bomb_summary.append(
            f"r{b['round']} t{b['tick']} {b['type']} by {b['player']} site={b['site']}"
        )

    round_end_summary = [f"r{re['round']} t{re['tick']}" for re in batch["round_ends"]]
    player_list = "\n".join(f"  {sid}: {name}{pro_marks.get(sid, '')}" for sid, name in players.items())

    prompt = f"""Create edit segments for a CS2 highlight reel.

MATCH: {action_timeline['map']}
ROUNDS: {batch['min_round']}-{batch['max_round']}
KILLS IN BATCH: {batch['kill_count']}

PLAYERS:
{player_list}

KILLS (local_index, tick, round, attacker->victim weapon):
{chr(10).join(kills_summary) if kills_summary else '(none)'}

BOMB EVENTS:
{chr(10).join(bomb_summary) if bomb_summary else '(none)'}

ROUND ENDS: {', '.join(round_end_summary) if round_end_summary else '(not available)'}

RULES:
- min 1 kill per segment; sorted by start_tick ascending
- OVERLAPS ALLOWED: multiple POVs in the same round are encouraged. Tick ranges MAY overlap.
  Example: Player A multi-kill segment, then ~2-3s later a Player B POV segment for their kills in the same round.
- NEVER span a round boundary: end_tick must be before the next round's start_tick
- SKIP BUY TIME on live rounds (round >= 1): start_tick must be >= round_freeze_end when available; fallback to round_start + 1536 (24s at 64 tick)
- WARMUP / KNIFE (round 0): no buy phase — start at round_start (freeze end), not +24s
- TICK RATE = 64 (FACEIT CS2): 1 second = 64 ticks. 6 seconds = 384 ticks.
- Use LOCAL indices from the KILLS list above (0-based within this batch)
- start_tick = anchor tick BEGINNING the segment; end_tick = anchor tick ENDING the segment
  - 1 kill: start = max(buy_end, kill_tick - 256), end = kill_tick + 128
  - 2+ kills: start = max(buy_end, first_kill_tick - 256), end = last_kill_tick + 128
  - NEVER set start_tick == end_tick
  - Each segment must span >= 384 ticks (6 seconds)
- ONE ATTACKER PER SEGMENT: different attackers in the same round → separate segments (one POV each). Never assign one segment's POV to another player's kills.
- ONE ROUND PER SEGMENT: never put kills from different rounds in the same segment.
- WARMUP ROUND: prefer one segment covering all knife/warmup kills (POV = attacker with most kills in that round).
- STREAK FRAGMENTS OK: the fix pass merges consecutive kills by the same attacker within each round (even if you split them across segments).
- MULTI-KILL PRIORITY: Recognised Pro with 2+ kills in quick succession (<=192 ticks / 3s apart) → multi_kill. Do NOT create a segment for another player's single kill whose tick falls strictly between that streak's first and last kill tick.
- POST-STREAK HANDOFF: After a Recognised Pro's 2+ kill streak in a round, do NOT insert a short solo segment for another player before the next real highlight — the next POV should start ~2-3s after the streak ends (overlapping tick ranges are fine).
- NOT EVERY KILL NEEDS A SEGMENT: trades and low-story solo frags between highlights may be omitted; the fix pass may drop them.
- Types: multi_kill (2+ kills same attacker quick succession), entry (first kill of round), clutch (1vX won), trade (teammate kill within 3s), utility (utility-defined play), default (fallback)
- POV = the attacker for that segment's kills (clutch winner for clutches; victim POV only for clutches)
- Recognised Pros (PRO) get POV priority; non-pro players may be POV only for their own multi-kills (2+ kills) — never create a 1-kill segment for a non-pro
- A single other-player kill must NOT split a Recognised Pro's multi-kill in the same round (merge pro POV; omit the interrupt)
- USE BOMB EVENTS to identify clutch/defuse scenarios
- Hard constraints (output will be rejected if violated):
  1. start_tick < end_tick
  2. (end_tick - start_tick) >= 384
  3. All kill_indices must be valid local indices from the KILLS list

{_edit_timeline_few_shot_examples()}
NOW EDIT THE REAL BATCH BELOW (use the KILLS list local indices, not the synthetic examples above).

OUTPUT JSON:
{{"segments":[{{"start_tick":0,"end_tick":0,"pov_steam_id":"","segment_type":"","kill_indices":[],"rationale":""}}]}}
"""
    return prompt


# ──────────────────────────────────────────────────────────────────────
# LLM call
# ──────────────────────────────────────────────────────────────────────

ZEN_SYS_MSG = "Output ONLY valid JSON. No markdown, no commentary."


def _try_model(
    client: openai.OpenAI,
    mdl: str,
    label: str,
    msg: str,
    retries: int = 3,
) -> str | None:
    import time
    for attempt in range(retries):
        try:
            resp = client.chat.completions.create(
                model=mdl,
                messages=[
                    {"role": "system", "content": ZEN_SYS_MSG},
                    {"role": "user", "content": msg},
                ],
                temperature=0.0,
                response_format={"type": "json_object"},
                max_tokens=16384,
                timeout=600,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            delay = min(5 * (2 ** attempt), 30)
            print(f"[WARN] {label} attempt {attempt + 1}/{retries} failed ({delay}s delay): {type(e).__name__}: {e}", file=sys.stderr)
            time.sleep(delay)
    return None


def _call_llm(prompt: str, model: str = DEFAULT_MODEL, retries: int = 3) -> str:
    if openai is None:
        raise RuntimeError("openai package not installed. pip install openai")

    zen_key = os.getenv("ZEN_API_KEY")
    or_key = os.getenv("OPENROUTER_API_KEY")

    zen_client = openai.OpenAI(base_url=ZEN_BASE_URL, api_key=zen_key) if zen_key else None
    or_client = openai.OpenAI(base_url=OPENROUTER_BASE_URL, api_key=or_key) if or_key else None

    if zen_client:
        result = _try_model(zen_client, model, f"zen/{model}", prompt, retries)
        if result is not None:
            return result

    if zen_client and model != FALLBACK_MODEL:
        print(f"[INFO] Zen primary failed, trying fallback {FALLBACK_MODEL}...", file=sys.stderr)
        result = _try_model(zen_client, FALLBACK_MODEL, f"zen/{FALLBACK_MODEL}", prompt, retries)
        if result is not None:
            return result

    if or_client:
        print(f"[INFO] Zen models failed, trying OpenRouter fallback {OPENROUTER_FALLBACK_MODEL}...", file=sys.stderr)
        result = _try_model(or_client, OPENROUTER_FALLBACK_MODEL, "openrouter/nemotron", prompt, retries)
        if result is not None:
            return result

    raise RuntimeError(
        f"All models failed: Zen ({model}, {FALLBACK_MODEL}) and OpenRouter ({OPENROUTER_FALLBACK_MODEL})"
    )


# ──────────────────────────────────────────────────────────────────────
# Validate + fix (operates on combined output)
# ──────────────────────────────────────────────────────────────────────

MIN_DURATION_TICKS = 384  # 6 seconds at 64 tick
MULTI_KILL_WINDOW_TICKS = 192  # 3s at 64 tick — quick succession / streak merge
WARMUP_ROUND = 0  # knife / warmup: no buy phase (round index in action_timeline)


def _segment_kill_ticks(seg: dict, kills: list[dict], max_idx: int) -> list[int]:
    return [kills[ki]["tick"] for ki in seg.get("kill_indices", []) if 0 <= ki <= max_idx]


def _segment_first_kill_tick(seg: dict, kills: list[dict], max_idx: int) -> int:
    ticks = _segment_kill_ticks(seg, kills, max_idx)
    return min(ticks) if ticks else int(seg.get("start_tick", 0))


def _segment_round(seg: dict, kills: list[dict], max_idx: int) -> int | None:
    rounds = {kills[ki].get("round") for ki in seg.get("kill_indices", []) if 0 <= ki <= max_idx}
    rounds.discard(None)
    if len(rounds) == 1:
        return next(iter(rounds))
    return None


def _segment_sole_attacker_sid(seg: dict, kills: list[dict], max_idx: int) -> str | None:
    sids = {kills[ki]["attacker_steam_id"] for ki in seg.get("kill_indices", []) if 0 <= ki <= max_idx}
    sids.discard(None)
    sids.discard("")
    if len(sids) == 1:
        return next(iter(sids))
    return None


def _split_segments_by_round(segments: list[dict], kills: list[dict], max_idx: int) -> list[dict]:
    """One segment must not span multiple rounds (LLM often lumps round boundaries)."""
    out: list[dict] = []
    for seg in segments:
        by_round: dict[int, list[int]] = {}
        for ki in seg.get("kill_indices", []):
            if not (0 <= ki <= max_idx):
                continue
            rn = kills[ki].get("round")
            if rn is None:
                continue
            by_round.setdefault(int(rn), []).append(ki)
        if len(by_round) <= 1:
            out.append(seg)
            continue
        for rn in sorted(by_round.keys()):
            kis = sorted(by_round[rn])
            sid = _segment_sole_attacker_sid({"kill_indices": kis}, kills, max_idx)
            out.append({
                **seg,
                "kill_indices": kis,
                "pov_steam_id": sid or seg.get("pov_steam_id", ""),
                "segment_type": "multi_kill" if len(kis) >= 2 else seg.get("segment_type", "default"),
                "rationale": f"Split by round {rn}: {seg.get('rationale', '')}",
            })
    return out


def _merge_pro_runs_through_solo_interrupts(
    runs: list[tuple[str, list[int]]],
    kills: list[dict],
    pro_sids: set[str],
) -> list[tuple[str, list[int]]]:
    """Merge a pro's kills across a single other-attacker kill in between (same round).

    Example: electroNic kill → TeSeS trade → electroNic molly becomes one electroNic segment.
    """
    if len(runs) < 3:
        return runs
    merged = True
    while merged:
        merged = False
        out: list[tuple[str, list[int]]] = []
        i = 0
        while i < len(runs):
            if i + 2 < len(runs):
                sid_a, kis_a = runs[i]
                sid_b, kis_b = runs[i + 1]
                sid_c, kis_c = runs[i + 2]
                if (
                    sid_a in pro_sids
                    and sid_a == sid_c
                    and sid_b != sid_a
                    and len(kis_b) == 1
                ):
                    out.append((sid_a, kis_a + kis_c))
                    i += 3
                    merged = True
                    continue
            out.append(runs[i])
            i += 1
        runs = out
    return runs


def _consolidate_attacker_runs_per_round(
    segments: list[dict],
    kills: list[dict],
    max_idx: int,
    warmup_round: int = 0,
) -> list[dict]:
    """Within each round, build segments from consecutive attacker runs in kill order.

    Warmup/knife rounds use a single segment for all kills in the round (no buy phase).
    """
    no_round: list[dict] = []
    by_round: dict[int, list[dict]] = {}
    for seg in segments:
        sr = _segment_round(seg, kills, max_idx)
        if sr is None:
            no_round.append(seg)
        else:
            by_round.setdefault(sr, []).append(seg)

    consolidated: list[dict] = []
    for sr in sorted(by_round.keys()):
        round_segs = by_round[sr]
        template = round_segs[0]
        kis_in_round = sorted(
            {
                ki
                for seg in round_segs
                for ki in seg.get("kill_indices", [])
                if 0 <= ki <= max_idx and kills[ki].get("round") == sr
            },
            key=lambda ki: kills[ki]["tick"],
        )
        if not kis_in_round:
            continue

        if sr == warmup_round:
            attacker_counts: dict[str, int] = {}
            for ki in kis_in_round:
                sid = kills[ki]["attacker_steam_id"] or ""
                attacker_counts[sid] = attacker_counts.get(sid, 0) + 1
            pov = max(attacker_counts, key=attacker_counts.get) if attacker_counts else template.get("pov_steam_id", "")
            consolidated.append({
                "start_tick": template.get("start_tick", kills[kis_in_round[0]]["tick"]),
                "end_tick": template.get("end_tick", kills[kis_in_round[-1]]["tick"]),
                "pov_steam_id": pov,
                "segment_type": "multi_kill" if len(kis_in_round) >= 2 else template.get("segment_type", "default"),
                "kill_indices": kis_in_round,
                "rationale": template.get("rationale", ""),
            })
            continue

        runs: list[tuple[str, list[int]]] = []
        for ki in kis_in_round:
            sid = kills[ki]["attacker_steam_id"] or ""
            if not runs or runs[-1][0] != sid:
                runs.append((sid, [ki]))
            else:
                runs[-1][1].append(ki)
        pro_sids = _get_pro_sids()
        runs = _merge_pro_runs_through_solo_interrupts(runs, kills, pro_sids)
        for sid, kis in runs:
            # Non-pro attackers need 2+ kills for a segment (no solo rando POV clips).
            if sid not in pro_sids and len(kis) < 2:
                continue
            consolidated.append({
                "start_tick": template.get("start_tick", kills[kis[0]]["tick"]),
                "end_tick": template.get("end_tick", kills[kis[-1]]["tick"]),
                "pov_steam_id": sid if sid else template.get("pov_steam_id", ""),
                "segment_type": "multi_kill" if len(kis) >= 2 else template.get("segment_type", "default"),
                "kill_indices": kis,
                "rationale": template.get("rationale", ""),
            })

    consolidated.extend(no_round)
    consolidated.sort(key=lambda s: _segment_first_kill_tick(s, kills, max_idx))
    return consolidated


def _drop_nonpro_solo_segments(
    segments: list[dict],
    kills: list[dict],
    max_idx: int,
    pro_sids: set[str],
) -> list[dict]:
    """Drop 1-kill segments whose sole attacker is not a Recognised Pro."""
    kept: list[dict] = []
    dropped = 0
    for seg in segments:
        kis = [ki for ki in seg.get("kill_indices", []) if 0 <= ki <= max_idx]
        if len(kis) != 1:
            kept.append(seg)
            continue
        sid = kills[kis[0]]["attacker_steam_id"] or ""
        if sid in pro_sids:
            kept.append(seg)
            continue
        dropped += 1
    if dropped:
        print(f"  [FIX] Removed {dropped} non-pro solo-kill segments", file=sys.stderr)
    return kept


def _sort_and_dedupe_segments(segments: list[dict]) -> list[dict]:
    """Sort kill_indices; drop duplicate segments (same kill set)."""
    for seg in segments:
        seg["kill_indices"] = sorted(set(seg.get("kill_indices", [])))
    seen_sets: set[frozenset[int]] = set()
    out: list[dict] = []
    for seg in segments:
        key = frozenset(seg.get("kill_indices", []))
        if not key or key in seen_sets:
            continue
        seen_sets.add(key)
        out.append(seg)
    return out


def _normalize_segment_types(segments: list[dict], kills: list[dict], max_idx: int) -> None:
    """Align segment_type with kill content (single attacker, multi-kill streak)."""
    for seg in segments:
        kis = [ki for ki in seg.get("kill_indices", []) if 0 <= ki <= max_idx]
        if len(kis) < 2:
            continue
        attackers = {kills[ki]["attacker_steam_id"] for ki in kis}
        if len(attackers) == 1:
            seg["segment_type"] = "multi_kill"


def _validate_edit_timeline(edit_tl: dict, action_timeline: dict, players: dict) -> list[str]:
    errors = []
    segments = edit_tl.get("segments", [])

    if not segments:
        errors.append("No segments produced")
        return errors

    for i, seg in enumerate(segments):
        if seg["start_tick"] >= seg["end_tick"]:
            errors.append(f"Segment {i}: start_tick ({seg['start_tick']}) >= end_tick ({seg['end_tick']}) — must be strictly less")
        duration = seg["end_tick"] - seg["start_tick"]
        if duration < MIN_DURATION_TICKS:
            errors.append(f"Segment {i}: duration {duration} ticks < {MIN_DURATION_TICKS} (6s minimum)")
        if i > 0 and seg["start_tick"] < segments[i - 1]["start_tick"]:
            errors.append(f"Segment {i}: not sorted by start_tick")

    max_kill_idx = len(action_timeline["kills"]) - 1
    for i, seg in enumerate(segments):
        for ki in seg["kill_indices"]:
            if not (0 <= ki <= max_kill_idx):
                errors.append(f"Segment {i}: kill_index {ki} out of range (0-{max_kill_idx})")

    for i, seg in enumerate(segments):
        if seg["pov_steam_id"] not in players:
            errors.append(f"Segment {i}: pov_steam_id {seg['pov_steam_id']} not in player list")

    for i, seg in enumerate(segments):
        kill_ticks = [action_timeline["kills"][ki]["tick"] for ki in seg["kill_indices"] if 0 <= ki <= max_kill_idx]
        if kill_ticks and seg["end_tick"] <= max(kill_ticks):
            errors.append(f"Segment {i}: end_tick ({seg['end_tick']}) <= last kill tick ({max(kill_ticks)})")

    for i, seg in enumerate(segments):
        if not seg["kill_indices"]:
            continue
        attacker_counts = {}
        for ki in seg["kill_indices"]:
            if 0 <= ki <= max_kill_idx:
                sid = action_timeline["kills"][ki]["attacker_steam_id"]
                attacker_counts[sid] = attacker_counts.get(sid, 0) + 1
        if attacker_counts:
            top_attacker = max(attacker_counts, key=attacker_counts.get)
            if seg["pov_steam_id"] != top_attacker and top_attacker in players:
                errors.append(f"Segment {i}: pov_steam_id {seg['pov_steam_id']} should be {top_attacker} (majority killer)")

    pro_sids = _get_pro_sids()
    pro_has_kills = any(k["attacker_steam_id"] in pro_sids for k in action_timeline["kills"])
    pro_pov_used = any(seg["pov_steam_id"] in pro_sids for seg in segments)
    if pro_has_kills and not pro_pov_used:
        errors.append("No Recognised Pro POV used despite PRO kills in timeline")

    for i, seg in enumerate(segments):
        if not seg["kill_indices"]:
            errors.append(f"Segment {i}: empty kill_indices")

    all_kills = set()
    for seg in segments:
        all_kills.update(seg["kill_indices"])
    # Not every kill needs a POV segment (e.g. solo trade sandwiched after a multi-kill streak).

    seen = set()
    dups = set()
    for seg in segments:
        for ki in seg["kill_indices"]:
            if ki in seen:
                dups.add(ki)
            seen.add(ki)
    if dups:
        errors.append(f"Duplicate kill indices: {sorted(dups)[:10]}")

    return errors


def _fix_edit_timeline(edit_tl: dict, action_timeline: dict, players: dict) -> dict:
    """Post-process LLM output to fix common validation errors."""
    segments = edit_tl.get("segments", [])
    if not segments:
        return edit_tl

    max_idx = len(action_timeline["kills"]) - 1
    kills = action_timeline["kills"]

    # Deduplicate kill indices across segments (first claim wins).
    seen: set[int] = set()
    for seg in segments:
        seg["kill_indices"] = [ki for ki in seg["kill_indices"] if not (ki in seen or seen.add(ki))]
    segments = [s for s in segments if s.get("kill_indices")]

    all_kills = set()
    for seg in segments:
        all_kills.update(seg["kill_indices"])
    pro_sids_early = _get_pro_sids()
    missing = sorted(set(range(max_idx + 1)) - all_kills)

    # Recover only omitted Recognised Pro kills; ambient non-pro frags may stay uncovered.
    for mk in missing:
        kill = kills[mk]
        if kill["attacker_steam_id"] not in pro_sids_early:
            continue
        pov = kill["attacker_steam_id"] or list(players.keys())[0]
        new_seg = {
            "start_tick": kill["tick"],
            "end_tick": kill["tick"],
            "pov_steam_id": pov,
            "segment_type": "default",
            "kill_indices": [mk],
            "rationale": f"Auto-inserted pro kill: {kill['attacker']} killed {kill['victim']}",
        }
        pos = next((i for i, s in enumerate(segments) if s["start_tick"] > kill["tick"]), len(segments))
        segments.insert(pos, new_seg)

    # Merge same-attacker multi-kill fragments that are <=window ticks apart (global adjacency).
    merged = True
    while merged:
        merged = False
        for i in range(len(segments) - 1):
            seg_a, seg_b = segments[i], segments[i + 1]
            attackers_a = {kills[ki]["attacker_steam_id"] for ki in seg_a["kill_indices"] if 0 <= ki <= max_idx}
            attackers_b = {kills[ki]["attacker_steam_id"] for ki in seg_b["kill_indices"] if 0 <= ki <= max_idx}
            if len(attackers_a) != 1 or attackers_a != attackers_b:
                continue
            ticks_a = [kills[ki]["tick"] for ki in seg_a["kill_indices"] if 0 <= ki <= max_idx]
            ticks_b = [kills[ki]["tick"] for ki in seg_b["kill_indices"] if 0 <= ki <= max_idx]
            if not ticks_a or not ticks_b:
                continue
            if min(ticks_b) - max(ticks_a) <= MULTI_KILL_WINDOW_TICKS:
                seg_a["kill_indices"] = sorted(set(seg_a["kill_indices"] + seg_b["kill_indices"]))
                seg_a["segment_type"] = "multi_kill"
                seg_a["rationale"] = f"Merged multi-kill: {seg_a['rationale']} + {seg_b['rationale']}"
                segments.pop(i + 1)
                merged = True
                break

    # Split multi-attacker segments into one segment per attacker (multi-POV per round).
    split_out: list[dict] = []
    for seg in segments:
        by_attacker: dict[str, list[int]] = {}
        for ki in seg["kill_indices"]:
            if not (0 <= ki <= max_idx):
                continue
            sid = kills[ki]["attacker_steam_id"] or seg.get("pov_steam_id") or ""
            by_attacker.setdefault(sid, []).append(ki)
        if len(by_attacker) <= 1:
            split_out.append(seg)
            continue
        for sid, kis in sorted(by_attacker.items(), key=lambda kv: min(kills[i]["tick"] for i in kv[1])):
            name = players.get(sid, sid)
            split_out.append({
                "start_tick": seg["start_tick"],
                "end_tick": seg["end_tick"],
                "pov_steam_id": sid if sid in players else seg["pov_steam_id"],
                "segment_type": "multi_kill" if len(kis) >= 2 else seg.get("segment_type", "default"),
                "kill_indices": sorted(kis),
                "rationale": f"Split POV for {name}: {seg.get('rationale', '')}",
            })
    segments = split_out
    segments = _split_segments_by_round(segments, kills, max_idx)
    segments = _consolidate_attacker_runs_per_round(segments, kills, max_idx, warmup_round=WARMUP_ROUND)
    segments = _sort_and_dedupe_segments(segments)
    _normalize_segment_types(segments, kills, max_idx)
    print(
        f"  [FIX] {len(segments)} segments after attacker split, round split, run consolidate",
        file=sys.stderr,
    )

    MIN_DURATION = MIN_DURATION_TICKS
    LEAD_TICKS = 256
    TAIL_TICKS = 128
    MULTI_TAIL_PER_KILL = 64
    BUY_TIME_TICKS = 1536  # 24s at 64 tick fallback when freeze_end is unavailable
    HANDOFF_TICKS = 160  # ~2.5s after prior POV's last kill

    round_start_by_round: dict[int, int] = {
        rs["round"]: int(rs["tick"])
        for rs in action_timeline.get("round_starts", [])
    }
    round_freeze_end_by_round: dict[int, int] = {
        rf["round"]: int(rf["tick"])
        for rf in action_timeline.get("round_freeze_ends", [])
    }

    def _seg_round(seg) -> int | None:
        return _segment_round(seg, kills, max_idx)

    def _next_round_start(seg_round: int | None) -> int | None:
        if seg_round is None:
            return None
        later = [t for r, t in round_start_by_round.items() if r > seg_round]
        return min(later) if later else None

    def _segment_floor(seg_round: int | None) -> int:
        """Earliest tick a segment may start in this round."""
        if seg_round is None or seg_round not in round_start_by_round:
            return 0
        rs = round_start_by_round[seg_round]
        if seg_round == WARMUP_ROUND:
            return rs  # warmup/knife: from round start (freeze end), no +24s buy
        if seg_round in round_freeze_end_by_round:
            return round_freeze_end_by_round[seg_round]
        return rs + BUY_TIME_TICKS

    def _play_start(seg_round: int | None) -> int | None:
        if seg_round is None or seg_round not in round_start_by_round:
            return None
        return _segment_floor(seg_round)

    def _anchor_window(seg, *, first_in_round: bool, prior_last_kill: int | None = None):
        kill_ticks = [kills[ki]["tick"] for ki in seg["kill_indices"] if 0 <= ki <= max_idx]
        if not kill_ticks:
            return seg["start_tick"], max(seg["end_tick"], seg["start_tick"] + MIN_DURATION)

        first_kill = min(kill_ticks)
        last_kill = max(kill_ticks)
        seg_round = _seg_round(seg)
        floor = _segment_floor(seg_round)
        play = _play_start(seg_round)

        if first_in_round and play is not None:
            desired_start = play
        else:
            lead = first_kill - LEAD_TICKS
            if prior_last_kill is not None:
                lead = min(lead, prior_last_kill + HANDOFF_TICKS)
            desired_start = max(floor, lead, 0)

        tail = TAIL_TICKS + MULTI_TAIL_PER_KILL * max(0, len(kill_ticks) - 1)
        desired_end = last_kill + tail

        nxt = _next_round_start(seg_round)
        if nxt is not None:
            # Hard stop: never render into the next round (no MIN_DURATION extension past this).
            desired_end = min(desired_end, nxt - 1)

        if desired_end <= last_kill and nxt is not None and last_kill < nxt:
            desired_end = min(last_kill + TAIL_TICKS, nxt - 1)

        if desired_end - desired_start < MIN_DURATION:
            if nxt is not None:
                desired_end = min(desired_start + MIN_DURATION, nxt - 1)
            else:
                desired_end = desired_start + MIN_DURATION
            if desired_end - desired_start < MIN_DURATION:
                desired_start = max(floor, desired_end - MIN_DURATION)

        return desired_start, desired_end

    def _anchor_all(segment_list: list[dict]) -> None:
        segment_list.sort(key=lambda s: (
            min((kills[ki]["tick"] for ki in s["kill_indices"] if 0 <= ki <= max_idx), default=s["start_tick"]),
            s.get("pov_steam_id", ""),
        ))
        last_kill_by_round: dict[int, int] = {}
        seen_round: set[int] = set()
        for seg in segment_list:
            sr = _seg_round(seg)
            first_in_round = sr is not None and sr not in seen_round
            if first_in_round:
                seen_round.add(sr)
            prior = last_kill_by_round.get(sr) if sr is not None else None
            ds, de = _anchor_window(seg, first_in_round=first_in_round, prior_last_kill=prior)
            seg["start_tick"] = ds
            seg["end_tick"] = de
            kticks = [kills[ki]["tick"] for ki in seg["kill_indices"] if 0 <= ki <= max_idx]
            if kticks and sr is not None:
                last_kill_by_round[sr] = max(last_kill_by_round.get(sr, 0), max(kticks))

    _anchor_all(segments)

    # POV = the (sole) attacker in the segment.
    for seg in segments:
        attacker_counts: dict[str, int] = {}
        for ki in seg["kill_indices"]:
            if 0 <= ki <= max_idx:
                sid = kills[ki]["attacker_steam_id"]
                attacker_counts[sid] = attacker_counts.get(sid, 0) + 1
        if attacker_counts:
            top = max(attacker_counts, key=attacker_counts.get)
            if top in players:
                seg["pov_steam_id"] = top

    # Drop solo-kill segments that fall inside another pro's multi-kill window.
    pro_sids = _get_pro_sids()
    multi_kill_windows = []
    for i, seg in enumerate(segments):
        if len(seg["kill_indices"]) < 2:
            continue
        attacker_sids = [kills[ki]["attacker_steam_id"] for ki in seg["kill_indices"] if 0 <= ki <= max_idx]
        if len(set(attacker_sids)) == 1 and attacker_sids[0] in pro_sids:
            kticks = [kills[ki]["tick"] for ki in seg["kill_indices"] if 0 <= ki <= max_idx]
            if kticks:
                multi_kill_windows.append({
                    "pro_sid": attacker_sids[0],
                    "first_tick": min(kticks),
                    "last_tick": max(kticks),
                    "segment_idx": i,
                })

    to_remove: set[int] = set()
    absorbed_kills: dict[int, list[int]] = {}
    for i, seg in enumerate(segments):
        if len(seg["kill_indices"]) != 1:
            continue
        ki = seg["kill_indices"][0]
        if not (0 <= ki <= max_idx):
            continue
        kill = kills[ki]
        if kill["attacker_steam_id"] not in pro_sids:
            continue
        for mw in multi_kill_windows:
            if mw["pro_sid"] != kill["attacker_steam_id"] and mw["first_tick"] <= kill["tick"] <= mw["last_tick"]:
                to_remove.add(i)
                absorbed_kills.setdefault(mw["segment_idx"], []).append(ki)
                break

    for mw_idx, kis in absorbed_kills.items():
        if mw_idx < len(segments):
            segments[mw_idx]["kill_indices"] = sorted(set(segments[mw_idx]["kill_indices"] + kis))
            segments[mw_idx]["segment_type"] = "multi_kill"

    if to_remove:
        segments = [s for i, s in enumerate(segments) if i not in to_remove]
        print(f"  [FIX] Removed {len(to_remove)} solo-kill segments inside multi-kill windows", file=sys.stderr)

    # Drop other-attacker solo segments sandwiched after a pro multi-kill streak before the next
    # highlight in the same round (narrative handoff; not every kill gets a clip).
    interrupt_remove: set[int] = set()
    by_round: dict[int, list[tuple[int, dict]]] = {}
    for i, seg in enumerate(segments):
        sr = _seg_round(seg)
        if sr is not None:
            by_round.setdefault(sr, []).append((i, seg))
    for _sr, items in by_round.items():
        items.sort(key=lambda pair: _segment_first_kill_tick(pair[1], kills, max_idx))
        for pos, (idx, seg) in enumerate(items):
            if idx in interrupt_remove:
                continue
            kis = [ki for ki in seg["kill_indices"] if 0 <= ki <= max_idx]
            if len(kis) < 2:
                continue
            attackers = {kills[ki]["attacker_steam_id"] for ki in kis}
            if len(attackers) != 1:
                continue
            streak_sid = next(iter(attackers))
            if streak_sid not in pro_sids:
                continue
            streak_last = max(kills[ki]["tick"] for ki in kis)
            k = pos + 1
            while k < len(items):
                j_idx, solo = items[k]
                skis = [ki for ki in solo["kill_indices"] if 0 <= ki <= max_idx]
                if len(skis) != 1:
                    break
                solo_kill = kills[skis[0]]
                if solo_kill["tick"] <= streak_last:
                    break
                if solo_kill["attacker_steam_id"] == streak_sid:
                    break
                if k + 1 >= len(items):
                    break  # last segment in round — keep solo
                interrupt_remove.add(j_idx)
                k += 1

    if interrupt_remove:
        segments = [s for i, s in enumerate(segments) if i not in interrupt_remove]
        print(
            f"  [FIX] Removed {len(interrupt_remove)} sandwiched solo segments after multi-kill streaks",
            file=sys.stderr,
        )

    # Drop empty / too-short after absorption; drop non-pro 1k clips; re-anchor.
    segments = [s for s in segments if s.get("kill_indices")]
    segments = _drop_nonpro_solo_segments(segments, kills, max_idx, pro_sids)
    segments = _sort_and_dedupe_segments(segments)
    _normalize_segment_types(segments, kills, max_idx)
    _anchor_all(segments)

    edit_tl["segments"] = segments
    return edit_tl


# ──────────────────────────────────────────────────────────────────────
# Main builder (batched)
# ──────────────────────────────────────────────────────────────────────

def build_edit_timeline(
    demo_path: Path | None,
    action_timeline_path: Path | None = None,
    model: str = DEFAULT_MODEL,
    batch_size: int = 5,
) -> dict:
    at_path = action_timeline_path
    if at_path is None:
        if demo_path is None:
            raise ValueError("Provide either demo_path or --action-timeline")
        if not _is_faceit_demo(demo_path):
            raise ValueError(f"FACEIT-only: demo must be under demos/faceit/ (got {demo_path})")
        at_path = _highlights_run_dir(demo_path) / "action_timeline.json"

    if not at_path.is_file():
        raise FileNotFoundError(f"Action Timeline not found: {at_path}. Run build_action_timeline.py first.")

    action_timeline = _load_action_timeline(at_path)
    players = _extract_players_from_action_timeline(action_timeline)

    batches = _split_into_round_batches(action_timeline, batch_size)
    total_rounds = max(r["round"] for r in action_timeline.get("round_starts", [])) if action_timeline.get("round_starts") else 0
    print(f"[INFO] {total_rounds} rounds -> {len(batches)} batches (batch_size={batch_size})", file=sys.stderr)

    all_segments = []
    for bi, batch in enumerate(batches):
        round_range = f"r{batch['min_round']}-r{batch['max_round']}"
        print(f"[BATCH {bi+1}/{len(batches)}] {round_range} ({batch['kill_count']} kills)...", file=sys.stderr)

        prompt = _build_batch_prompt(batch, action_timeline, players)

        try:
            llm_output = _call_llm(prompt, model=model, retries=2)
            batch_result = json.loads(llm_output)
        except (json.JSONDecodeError, RuntimeError) as e:
            print(f"[WARN] Batch {bi+1} failed ({e}), skipping", file=sys.stderr)
            continue

        batch_segments = batch_result.get("segments", [])

        # Remap local kill indices -> global indices
        global_indices = batch["global_kill_indices"]
        for seg in batch_segments:
            seg["kill_indices"] = [
                global_indices[li] for li in seg["kill_indices"]
                if li < len(global_indices)
            ]

        all_segments.extend(batch_segments)
        print(f"  -> {len(batch_segments)} segments", file=sys.stderr)

    # Sort combined segments by start_tick
    all_segments.sort(key=lambda s: s["start_tick"])

    edit_tl = {
        "demo_path": action_timeline["demo_path"],
        "map": action_timeline["map"],
        "segments": all_segments,
    }

    # Always run post-LLM fixes (anchoring, merges, POV correction, buy-time).
    # Validation alone misses cases like round-0 truthiness (start too late but "valid").
    errors = _validate_edit_timeline(edit_tl, action_timeline, players)
    if errors:
        print(f"[INFO] Fixing {len(errors)} validation issues post-LLM", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
    edit_tl = _fix_edit_timeline(edit_tl, action_timeline, players)
    errors_after = _validate_edit_timeline(edit_tl, action_timeline, players)
    if errors_after:
        print(f"[WARN] {len(errors_after)} issues remain after fix: {errors_after}", file=sys.stderr)
    elif errors:
        print(f"[OK] All issues fixed post-LLM", file=sys.stderr)

    return edit_tl


def main() -> int:
    ap = argparse.ArgumentParser(description="Build Edit Timeline from Action Timeline (LLM-driven, batched)")
    ap.add_argument("demo_path", type=Path, nargs="?", help="Path to FACEIT .dem under demos/faceit/")
    ap.add_argument("--action-timeline", type=Path, help="action_timeline.json path")
    ap.add_argument(
        "--edit-timeline",
        type=Path,
        help="Existing edit_timeline.json (raw LLM segments) to re-run _fix_edit_timeline only",
    )
    ap.add_argument(
        "--fix-only",
        action="store_true",
        help="Skip LLM; load --edit-timeline + --action-timeline, apply _fix_edit_timeline, write output",
    )
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"Model to use (default: {DEFAULT_MODEL})")
    ap.add_argument("--batch-size", type=int, default=5, help="Rounds per LLM batch (default: 5)")
    ap.add_argument("--output", type=Path, help="Override output path (default: renders/hl-{stem}/edit_timeline.json)")
    args = ap.parse_args()

    demo = args.demo_path

    if args.fix_only:
        if not args.action_timeline or not args.edit_timeline:
            ap.error("--fix-only requires --action-timeline and --edit-timeline")
        if not args.action_timeline.is_file():
            print(f"[ERR] action timeline not found: {args.action_timeline}", file=sys.stderr)
            return 1
        if not args.edit_timeline.is_file():
            print(f"[ERR] edit timeline not found: {args.edit_timeline}", file=sys.stderr)
            return 1
        action_timeline = _load_action_timeline(args.action_timeline)
        players = _extract_players_from_action_timeline(action_timeline)
        edit_tl = json.loads(args.edit_timeline.read_text(encoding="utf-8"))
        edit_tl = _fix_edit_timeline(edit_tl, action_timeline, players)
        out = args.output or args.edit_timeline
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(edit_tl, indent=2), encoding="utf-8")
        print(f"[OK] Fixed edit timeline -> {out} ({len(edit_tl['segments'])} segments)")
        return 0

    if args.action_timeline:
        try:
            edit_tl = build_edit_timeline(demo, args.action_timeline, args.model, args.batch_size)
        except Exception as e:
            print(f"[ERR] {e}", file=sys.stderr)
            return 1
        out = args.output or (args.action_timeline.parent / "edit_timeline.json")
    else:
        if demo is None:
            ap.error("Provide either demo_path or --action-timeline")
        if not demo.is_file():
            print(f"[ERR] demo not found: {demo}", file=sys.stderr)
            return 1
        try:
            edit_tl = build_edit_timeline(demo, model=args.model, batch_size=args.batch_size)
        except Exception as e:
            print(f"[ERR] {e}", file=sys.stderr)
            return 1
        out = args.output or (_highlights_run_dir(demo) / "edit_timeline.json")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(edit_tl, indent=2), encoding="utf-8")
    print(f"[OK] Edit Timeline -> {out} ({len(edit_tl['segments'])} segments)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
