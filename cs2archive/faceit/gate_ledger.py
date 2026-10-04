"""Gate decision shadow ledger: one JSONL line per FACEIT scrape.

Answers "gate said no" vs "player never played" after the fact. Each scrape
writes a summary line (counts + qualified players with reasons) plus one
detail line per candidate carrying any demand signal (index entry or
breakout) — the long tail of never-measured players is counted, not
listed, to bound volume.

Path: ``.data/gate_decisions.jsonl`` (best-effort append; never raises).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from cs2archive import scoring as _scoring
from cs2archive.faceit.scrape_notable import FACEIT_STAR_FLOOR

RULE_VERSION = _scoring.DEMAND_RULE_VERSION

ROOT = Path(__file__).resolve().parents[2]
LEDGER_PATH = ROOT / ".data" / "gate_decisions.jsonl"


def gate_verdict(candidate: dict) -> dict:
    """Verdict + reasons for one candidate, read from the live payload.

    ``verdict`` mirrors the production gate (demand star OR breakout,
    F3 — previously demand-star-only, which logged every
    breakout-passed player as a reject and corrupted "gate said no"
    vs "never played"). ``breakout_or`` still records the spike arm
    separately for measurement. Includes stream + rule version for
    post-hoc dedupe and attribution.
    """
    nick = str(candidate.get("player") or "")
    star, spike, index, supported, pi = _scoring.demand_eligibility(
        nick, None, star_floor=FACEIT_STAR_FLOOR)
    passed = bool(star or spike)
    if star:
        reason = f"star(ix={index})"
    elif spike:
        reason = f"breakout(pi={pi})"
    else:
        reason = "reject"
    return {
        "player": nick,
        "match_id": candidate.get("match_id"),
        "stream": candidate.get("stream"),
        "date": str(candidate.get("date") or "")[:10],
        "rule": f"star-or-breakout@{RULE_VERSION}",
        "verdict": "pass" if passed else "reject",
        "reason": reason,
        "index": index,
        "supported": supported,
        "breakout_or": bool(spike),
        "breakout_pi": pi,
    }


def _event_id(source: str, verdict: dict) -> str:
    base = (verdict.get("match_id") or verdict.get("player") or "?")
    return f"{source}:{base}:{verdict.get('player') or '?'}".replace(" ", "_")


def record_gate_scrape(source: str, candidates: list[dict],
                       path: Path = LEDGER_PATH) -> None:
    """Append one summary line + signal-carrying detail lines. Never raises."""
    try:
        verdicts = [gate_verdict(c) for c in candidates]
        qualified = [v for v in verdicts if v["verdict"] == "pass"]
        detail = [v for v in verdicts
                  if v["verdict"] == "pass" or v["index"] is not None
                  or v["breakout_pi"] is not None]
        stamp = datetime.now(timezone.utc).isoformat()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "ts": stamp,
                "source": source,
                "kind": "scrape",
                "rule": f"demand-only@{RULE_VERSION}",
                "candidates": len(candidates),
                "qualified": len(qualified),
                "qualified_players": [
                    {"player": v["player"], "reason": v["reason"]}
                    for v in qualified],
            }) + "\n")
            for v in detail:
                handle.write(json.dumps(
                    {"ts": stamp, "source": source, "kind": "verdict",
                     "event_id": _event_id(source, v), **v}) + "\n")
    except Exception:
        pass
