"""Build Edit Timeline from Action Timeline using LLM (FACEIT only).

Reads Action Timeline, splits into round batches, prompts LLM per batch,
concatenates results, validates output, writes edit_timeline.json.

Usage:
    python scripts/highlights/build_edit_timeline.py demos/faceit/<demo>.dem
    python scripts/highlights/build_edit_timeline.py --action-timeline renders/hl-<stem>/action_timeline.json
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
- non-overlapping sequential segments, min 1 kill per segment
- sorted by start_tick ascending; gaps between segments OK
- TICK RATE = 64 (FACEIT CS2): 1 second = 64 ticks. 10 seconds = 640 ticks.
- Use LOCAL indices from the KILLS list above (0-based within this batch)
- start_tick = anchor tick BEGINNING the segment; end_tick = anchor tick ENDING the segment
  - 1 kill: start = max(0, kill_tick - 320), end = kill_tick + 320
  - 2+ kills: start = first_kill_tick - 320, end = last_kill_tick + 320
  - NEVER set start_tick == end_tick
  - Each segment must span >= 640 ticks (10 seconds)
- Types: multi_kill (2+ kills same attacker quick succession), entry (first kill of round), clutch (1vX won), trade (teammate kill within 3s), utility (utility-defined play), default (fallback)
- POV priority: attacker for multi_kill/entry, clutch winner, trade-killer, default=recent killer
- Recognised Pros (PRO) get POV priority; unknowns only if no PRO in segment
- Victim POV only for clutches
- USE BOMB EVENTS to identify clutch/defuse scenarios
- Hard constraints (output will be rejected if violated):
  1. start_tick < end_tick
  2. (end_tick - start_tick) >= 640
  3. All kill_indices must be valid local indices from the KILLS list

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

def _validate_edit_timeline(edit_tl: dict, action_timeline: dict, players: dict) -> list[str]:
    errors = []
    segments = edit_tl.get("segments", [])

    if not segments:
        errors.append("No segments produced")
        return errors

    MIN_DURATION_TICKS = 384  # 6 seconds — matches the fixer/anchor floor
    for i, seg in enumerate(segments):
        if seg["start_tick"] >= seg["end_tick"]:
            errors.append(f"Segment {i}: start_tick ({seg['start_tick']}) >= end_tick ({seg['end_tick']}) — must be strictly less")
        duration = seg["end_tick"] - seg["start_tick"]
        if duration < MIN_DURATION_TICKS:
            errors.append(f"Segment {i}: duration {duration} ticks < {MIN_DURATION_TICKS} (10s minimum)")
        if i > 0:
            prev = segments[i - 1]
            overlap = prev["end_tick"] - seg["start_tick"] + 1
            if overlap > 1:
                errors.append(f"Segment {i}: overlaps previous by {overlap} ticks")
            if seg["start_tick"] < prev["start_tick"]:
                errors.append(f"Segment {i}: not sorted")

    max_kill_idx = len(action_timeline["kills"]) - 1
    for i, seg in enumerate(segments):
        for ki in seg["kill_indices"]:
            if not (0 <= ki <= max_kill_idx):
                errors.append(f"Segment {i}: kill_index {ki} out of range (0-{max_kill_idx})")

    for i, seg in enumerate(segments):
        if seg["pov_steam_id"] not in players:
            errors.append(f"Segment {i}: pov_steam_id {seg['pov_steam_id']} not in player list")

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
    missing = set(range(max_kill_idx + 1)) - all_kills
    if missing:
        errors.append(f"Kills not in any segment: {len(missing)} total (first few: {sorted(missing)[:10]})")

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

    seen = set()
    for seg in segments:
        seg["kill_indices"] = [ki for ki in seg["kill_indices"] if not (ki in seen or seen.add(ki))]

    all_kills = set()
    for seg in segments:
        all_kills.update(seg["kill_indices"])
    missing = sorted(set(range(max_idx + 1)) - all_kills)

    for mk in missing:
        kill = action_timeline["kills"][mk]
        pov = kill["attacker_steam_id"] or list(players.keys())[0]
        new_seg = {
            "start_tick": kill["tick"],
            "end_tick": kill["tick"],
            "pov_steam_id": pov,
            "segment_type": "default",
            "kill_indices": [mk],
            "rationale": f"Auto-inserted: {kill['attacker']} killed {kill['victim']}",
        }
        pos = next((i for i, s in enumerate(segments) if s["start_tick"] > kill["tick"]), len(segments))
        segments.insert(pos, new_seg)

    MIN_DURATION_TICKS = 384
    LEAD_TICKS = 256
    TAIL_TICKS = 128
    MULTI_TAIL_PER_KILL = 64
    ROUND_START_GRACE = 128

    round_start_by_round: dict[int, int] = {
        rs["round"]: int(rs["tick"])
        for rs in action_timeline.get("round_starts", [])
    }

    def _anchor_window(seg):
        kill_ticks = [
            action_timeline["kills"][ki]["tick"]
            for ki in seg["kill_indices"]
            if 0 <= ki <= max_idx
        ]
        if not kill_ticks:
            return seg["start_tick"], max(seg["end_tick"], seg["start_tick"] + MIN_DURATION_TICKS)

        first_kill_tick = min(kill_ticks)
        last_kill_tick = max(kill_ticks)
        kill_count = len(kill_ticks)

        seg_round = action_timeline["kills"][seg["kill_indices"][0]].get("round")
        round_anchor = 0
        if seg_round and seg_round in round_start_by_round:
            round_anchor = round_start_by_round[seg_round] + ROUND_START_GRACE

        lead_anchor = max(0, first_kill_tick - LEAD_TICKS)
        desired_start = max(round_anchor, lead_anchor)

        tail = TAIL_TICKS + MULTI_TAIL_PER_KILL * max(0, kill_count - 1)
        desired_end = last_kill_tick + tail
        desired_end = max(desired_end, desired_start + MIN_DURATION_TICKS)
        return desired_start, desired_end

    for seg in segments:
        ds, de = _anchor_window(seg)
        seg["start_tick"] = ds
        seg["end_tick"] = de

    n = len(segments)
    for _ in range(n + 5):
        changed = False
        for i, seg in enumerate(segments):
            ds, de = _anchor_window(seg)
            prev_end = segments[i - 1]["end_tick"] if i > 0 else -1
            nxt_start = segments[i + 1]["start_tick"] if i + 1 < n else None

            new_start = max(ds, prev_end + 1 if i > 0 else 0)

            if nxt_start is not None and de >= nxt_start:
                new_end = nxt_start - 1
                if new_end - new_start < MIN_DURATION_TICKS and i + 1 < n:
                    new_end = new_start + MIN_DURATION_TICKS
            else:
                new_end = de

            if new_end - new_start < MIN_DURATION_TICKS:
                earliest_start = (prev_end + 1) if i > 0 else 0
                new_start = max(earliest_start, new_end - MIN_DURATION_TICKS)
                if new_end - new_start < MIN_DURATION_TICKS:
                    new_end = new_start + MIN_DURATION_TICKS

            if seg["start_tick"] != new_start or seg["end_tick"] != new_end:
                seg["start_tick"] = new_start
                seg["end_tick"] = new_end
                changed = True
        if not changed:
            break

    # Final pass: expand first segment of each round to start at round_start + 16s (buy time).
    # Must come AFTER all anchoring/deconfliction so it isn't overwritten.
    BUY_TIME_TICKS = 1280  # 20 seconds at 64 tick
    _seen: set[int] = set()
    for seg in segments:
        if not seg["kill_indices"]:
            continue
        sr = action_timeline["kills"][seg["kill_indices"][0]].get("round")
        if sr and sr not in _seen and sr in round_start_by_round:
            desired = round_start_by_round[sr] + BUY_TIME_TICKS
            if desired < seg["start_tick"]:
                seg["start_tick"] = desired
            _seen.add(sr)

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

    # Validate + fix combined output
    errors = _validate_edit_timeline(edit_tl, action_timeline, players)
    if errors:
        print(f"[INFO] Fixing {len(errors)} validation issues post-LLM", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        edit_tl = _fix_edit_timeline(edit_tl, action_timeline, players)
        errors_after = _validate_edit_timeline(edit_tl, action_timeline, players)
        if errors_after:
            print(f"[WARN] {len(errors_after)} issues remain after fix: {errors_after}", file=sys.stderr)
        else:
            print(f"[OK] All issues fixed post-LLM", file=sys.stderr)

    return edit_tl


def main() -> int:
    ap = argparse.ArgumentParser(description="Build Edit Timeline from Action Timeline (LLM-driven, batched)")
    ap.add_argument("demo_path", type=Path, nargs="?", help="Path to FACEIT .dem under demos/faceit/")
    ap.add_argument("--action-timeline", type=Path, help="Override action_timeline.json path")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"Model to use (default: {DEFAULT_MODEL})")
    ap.add_argument("--batch-size", type=int, default=5, help="Rounds per LLM batch (default: 5)")
    ap.add_argument("--output", type=Path, help="Override output path (default: renders/hl-{stem}/edit_timeline.json)")
    args = ap.parse_args()

    demo = args.demo_path

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
