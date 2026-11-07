"""Run the Allstar picker agent and turn its answer into a render order.

The agent picks one clip to publish from a five-clip shortlist. It is given the
demo facts (see :mod:`cs2archive.shorts.picker_nominations`) and unrestricted
web research, and it must cite evidence for every claim.

Two things this module is careful about:

* **The prompt is the product.** Without the demo facts every model tested
  ranked narrative ("grand final", "underdog") and got in-game facts wrong —
  one claimed a 13-2 map that was 13-6, another an elimination loss that was a
  2-1 series win. With the facts they ranked gameplay and their numbers checked
  out. Do not ship a picker without the facts layer.
* **A pick is a claim, not a fact.** ``parse_pick`` validates every clip id
  against the nominations it was given. An id the agent invented is dropped,
  not passed to the render queue.

Usage:
    python -m cs2archive.shorts.picker_agent --nominations .data/allstar_picker/nominations.json
    python -m cs2archive.shorts.picker_agent --nominations <path> --model glm --timeout 3600
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

PI_ENTRY = (Path(os.environ.get("APPDATA", "")) / "npm" / "node_modules" /
            "@earendil-works" / "pi-coding-agent" / "dist" / "bundle" / "cli.js")

# slug -> (provider, model, thinking). Chosen from the picker bake-off: ultra and
# super were fast and agreed; glm was slow but caught more (eco farms, dead
# gaps); dsv4f paged through the payload instead of researching.
MODELS = {
    "ultra": ("openrouter", "nvidia/nemotron-3-ultra-550b-a55b:free", "low"),
    "super": ("openrouter", "nvidia/nemotron-3.5-super-vl:free", "low"),
    "glm": ("nvidia-nim", "glm-5.3-flash", "low"),
    "dsv4f": ("nvidia-nim", "deepseek-ai/deepseek-v4.1-flash", "low"),
}

# Research tools only: no bash/edit/write. The picker must not touch the repo
# and must not be able to render or upload.
TOOLS = "web_search,fetch_content,google_search,get_search_content,read"

PROMPT = """You are picking the next YouTube Short for a CS2 highlight channel.

The channel publishes one Short per day: a single player-POV clip from a pro
match, cut to 9:16, with that player's own HUD (crosshair, viewmodel, aspect).
It competes for attention against every other CS2 clip posted that day.

## Your job

Choose the one clip to publish. The shortlist is the five most-viewed eligible
Allstar clips from the last week — a popularity prior, not a verdict. Your job
is to decide which of them actually makes the better Short, and to say why the
other four lose. Justify it with evidence.

Five clips is a small enough set to open every facts file, so do that rather
than skimming the index. Depth beats breadth here.

## Input

The nominations file is attached. It is an **index**: one entry per clip, with
enough to rank without opening anything. Per clip:

- `clip_id`, `player`, `steamid`, `opponent`, `match_id`, `hltv_match` (URL)
- `map`, `round`, `kinds` (Allstar's labels: ace / 4k / 1v3_won / wallbang)
- `title` (Allstar's label for the play)
- `views` — the newest Allstar snapshot
- `views_per_hour` — lifetime views over the clip's real age, so it IS
  age-normalised and comparable across clips. `recent_views_per_hour` is growth
  between snapshots and is null until two snapshots are far enough apart.
- `clip_length` — the clip's duration in seconds
- `score_line`, `series_game_label` — the map score and which map of the series
- `local_demo_folder` — local demo directory, or null
- `facts_summary` — the round's key numbers, extracted from the demo
- `facts_file` — path to the full per-kill detail for this clip

### `facts_summary` and `facts_file`

`facts_summary` is the same evidence a human reviewer uses, condensed: kill
count, headshots, kill span, gaps between kills, buy type, pistol round,
deficit, whether the round was won, the score after it, the weapons involved,
through-smoke and wallbang counts, max distance, and the series result.

Open `facts_file` for the clips you are seriously weighing. It adds:

- `equip_value`: per-team equipment value at round start
- `win_reason`, `score_after_round`
- `kills[]`: per kill — `t_after_first_s`, `victim`, `weapon`, `victim_weapon`,
  `headshot`, `hitgroup`, `damage`, `distance_m`, `through_smoke`,
  `penetrated`, `attacker_place`, `victim_place`
- `other_kills_in_round[]`: the rest of the round, so you can see the sequence
- `series[]`: every map of the series with its final score and whether the
  player's team won it. Use this for the match result instead of trusting a
  web summary — if a source contradicts `series`, the demo wins.

`facts_summary: null` means the demo is not local. Say so; do not guess the round.

What this does NOT tell you: how the clip is cut, camera work, whether the
player's aim looks impressive on screen, or how the moment reads at 9:16.
You cannot watch the video. Do not claim to have watched it.

### About `views`

`views` is one snapshot, not a lifetime total. `views_per_hour` is the honest
rate: lifetime views over the clip's real age. It is still a weak signal — a
clip can be popular and a bad Short — so never let it decide on its own.

## Research

You have web tools. Use them. For the clips that matter to your ranking, find:

- the event, its stage, and what was at stake in that match
- the opponent's real strength (ranking, form), not just their name
- the player's standing and current form
- whether this exact play was already covered heavily by other channels
  (search the clip sites and YouTube; say where you looked)
- anything about the round or match that changes how impressive the play is

Anything you assert about the scene must come from a source you read, and you
must give the URL. If a source and the demo disagree, the demo wins for
in-game facts and you should say that they disagree.

## Rules

- Rank on evidence, not vibes. Gameplay claims must cite `facts_summary` or
  `facts_file`; scene claims must cite a URL.
- Do not invent facts. Mark anything unverified as unverified.
- Do not claim to have watched any clip.
- A clip can be technically huge and still a bad Short (slow, unclear, already
  everywhere, no stakes). Say so when that is your read.
- Copy `clip_id` values verbatim from the input. Never abbreviate or reconstruct
  them.
- No rendering, no uploading, no file edits. Research and decide only.

## Output

1. **Ranking** — the shortlist, best first, one line each:
   `N. <clip_id> — player, map, kind — reason`
2. **My pick** — the one clip you would publish, 2-4 sentences: why it beats
   the runner-up.
3. **Evidence table** — the decisive claims, one row each:
   `claim | facts_summary/facts_file field or URL | verified?`
4. **Scene notes** — what your research changed about your read.
5. **Unverified** — what you could not confirm, and how much it would move your
   ranking if it went the other way.
6. **Runner-up case** — the strongest argument against your own pick.

Then, as the very last thing in your answer, one fenced ```json block:

```json
{"ranking": ["<clip_id>", "<clip_id>"], "pick": "<clip_id>", "reason": "<one sentence>"}
```

`ranking` must list every shortlisted clip id, best first, copied verbatim.
"""

_JSON_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)


def parse_pick(text: str, valid_ids: set[str] | None = None) -> dict | None:
    """Extract the machine-readable pick from the agent's answer.

    The last fenced JSON block wins — the prompt asks for it at the end, and a
    model that restates an earlier draft must not override its own conclusion.
    Ids are validated against ``valid_ids`` when given: an invented clip id is
    dropped rather than handed to the render queue.
    """
    blocks = _JSON_BLOCK.findall(text or "")
    for raw in reversed(blocks):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        ranking = [str(c) for c in payload.get("ranking") or [] if c]
        pick = str(payload.get("pick") or "") or None
        if valid_ids is not None:
            ranking = [c for c in ranking if c in valid_ids]
            if pick not in valid_ids:
                pick = None
        if not ranking and not pick:
            continue
        return {
            "ranking": ranking,
            "pick": pick or (ranking[0] if ranking else None),
            "reason": str(payload.get("reason") or ""),
        }
    return None


def _final_text(stdout: str) -> str:
    """Concatenate assistant text parts from Pi's JSON event stream."""
    chunks: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "message_end":
            continue
        message = event.get("message") or {}
        if message.get("role") != "assistant":
            continue
        for part in message.get("content") or []:
            if part.get("type") == "text" and part.get("text"):
                chunks.append(part["text"])
    if chunks:
        return "\n".join(chunks)
    return stdout if stdout and not stdout.lstrip().startswith("{") else ""


def run_picker(
    nominations: Path,
    *,
    model: str = "glm",
    timeout: int = 3600,
    transcript: Path | None = None,
    thinking: str | None = None,
) -> dict:
    """Run the picker headless and return its answer plus the parsed pick.

    The transcript is streamed to disk as it arrives, so a run killed at the
    wall-clock cap still leaves evidence of what the model was doing — a
    looping model and a slow one are otherwise indistinguishable.
    """
    if model not in MODELS:
        raise ValueError(f"unknown model slug {model!r}; known: {sorted(MODELS)}")
    provider, model_name, default_thinking = MODELS[model]
    nominations = Path(nominations)
    if not nominations.is_file():
        raise FileNotFoundError(nominations)
    payload = json.loads(nominations.read_text(encoding="utf-8"))
    valid_ids = {str(c.get("clip_id")) for c in payload.get("nominations") or []}

    message = (
        f"{PROMPT}\n\n---\n\nNominations file: {nominations.name}\n"
        f"Read it with the read tool at this absolute path:\n{nominations.resolve()}\n"
    )
    node = shutil.which("node") or "node"
    cmd = [
        node, str(PI_ENTRY), "-p", "--no-session", "--mode", "json",
        "--provider", provider, "--model", model_name,
        "--thinking", thinking or default_thinking,
        "--tools", TOOLS,
        message,
    ]
    transcript = Path(transcript) if transcript else nominations.with_suffix(".transcript.jsonl")
    transcript.parent.mkdir(parents=True, exist_ok=True)

    started = time.time()
    lines: list[str] = []
    timed_out = False
    with transcript.open("w", encoding="utf-8") as sink:
        proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                text=True, encoding="utf-8", errors="replace",
                                bufsize=1)
        try:
            for line in proc.stdout:
                sink.write(line)
                sink.flush()
                lines.append(line)
                if time.time() - started > timeout:
                    timed_out = True
                    proc.kill()
                    break
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
        stderr = (proc.stderr.read() if proc.stderr else "") or ""
        if proc.stderr:
            proc.stderr.close()
    elapsed = time.time() - started

    text = _final_text("".join(lines))
    return {
        "model": model,
        "provider": provider,
        "model_name": model_name,
        "elapsed": round(elapsed),
        "exit": "timeout" if timed_out else proc.returncode,
        "text": text,
        "pick": parse_pick(text, valid_ids),
        "transcript": str(transcript),
        "stderr": stderr[-2000:],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the Allstar picker agent")
    ap.add_argument("--nominations", type=Path, required=True)
    ap.add_argument("--model", default="glm", choices=sorted(MODELS))
    ap.add_argument("--timeout", type=int, default=3600,
                    help="Wall-clock cap per run; dsv4f needs the full hour")
    ap.add_argument("--transcript", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None, help="Write the full result JSON here")
    args = ap.parse_args()

    result = run_picker(args.nominations, model=args.model, timeout=args.timeout,
                        transcript=args.transcript)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    summary = {k: v for k, v in result.items() if k not in ("text", "stderr")}
    print(json.dumps(summary, indent=2))
    if result["pick"] is None:
        print("[picker] no machine-readable pick in the answer", flush=True)
        return 2
    print(f"[picker] pick: {result['pick']['pick']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
