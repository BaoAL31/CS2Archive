"""Score the player star system against scraped competitor POV performance.

Ground truth is the POV-market scrape (``exports/pov_market/video_history.csv``,
see ``cs2archive/misc/scrape_pov_channels.py``): public view counts for competing
CS2 POV channels plus ``@cs2povarchive``. The target per video is
``performance_index`` = views/day divided by its own channel's median views/day —
the exact quantity the star fit is built from, so it is the honest yardstick.

Protocol (no leakage):

1. Load + dedupe the scrape, keep Recognised-Pro videos (>= 300s, >= 2 days old).
2. Fit window = videos published strictly before the cutoff
   (``--eval-days`` before now). The index is rebuilt **as of that cutoff** by
   the production code path (``update_player_demand`` helpers), so only past
   performance feeds the star.
3. Held-out = videos published at/after the cutoff. Each is scored with the
   fitted star (unknown player -> neutral 1.0, exactly as production does).
4. Report rank correlation, star-band lift, the ``>= 1.25`` gate's precision /
   recall / lift over the base rate, and a threshold sweep.

``--mode in-sample`` instead scores the shipped ``.data/player_demand_index.json``
over the whole scrape. Those numbers are circular by construction (the index is
estimated from these same videos) and the report says so; it is only useful to
compare the shipped file against the rebuilt one.

Usage:
    python -m cs2archive.hltv.eval_stars
    python -m cs2archive.hltv.eval_stars --eval-days 60
    python -m cs2archive.hltv.eval_stars --out exports/pov_market/star_eval.json
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[2]

from cs2archive.faceit.update_player_demand import (  # noqa: E402
    DEMAND_PATH,
    RECENT_DAYS,
    WINDOW_DAYS,
    build_index,
    canonical_player,
    in_window,
    load_seed_rows,
    recognised_aliases,
    refresh_velocity,
)
from cs2archive.misc.analyze_pov_market import analyze_rows  # noqa: E402
from cs2archive.shorts.fit_clip_weights import spearman  # noqa: E402

OUT_DEFAULT = ROOT / "exports" / "pov_market" / "star_eval.json"
STAR_FLOOR = 1.25
NEUTRAL_STAR = 1.0
MIN_DURATION_S = 300
MIN_AGE_DAYS = 2.0
MIN_EVAL_DAYS = 14
# A fold this thin cannot fit a star at all (production needs 8 long-window or 3
# recent videos per player, so a scoreboard-sized fold yields an empty index).
MIN_FIT_VIDEOS = 20
MIN_HOLDOUT_VIDEOS = 10
# Star bands used for the lift table. ``(lower, upper, label)``; None is open.
BANDS = (
    (None, 1.0, "<1.00"),
    (1.0, 1.08, "1.00 no star"),
    (1.08, 1.25, "1.08-1.24"),
    (1.25, 1.50, "1.25-1.49"),
    (1.5, None, "1.50+"),
)
SWEEP_THRESHOLDS = tuple(round(1.0 + 0.05 * i, 2) for i in range(17))  # 1.00 .. 1.80


def _num(value) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _pearson(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 3:
        return None
    xs, ys = zip(*pairs)
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    cov = sum((x - mx) * (y - my) for x, y in pairs)
    denom = math.sqrt(
        sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys)
    )
    return round(cov / denom, 4) if denom else None


def _quantile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def load_videos(*, history_path: Path | None = None, now: datetime | None = None) -> list[dict]:
    """Dedupe the scrape by video_id (newest capture wins) and re-age it.

    Defaults to production's union of seed CSVs (``load_seed_rows``); pass
    ``history_path`` to eval a single file.
    """
    import csv

    if history_path is not None:
        with Path(history_path).open(encoding="utf-8-sig", newline="") as handle:
            raw = list(csv.DictReader(handle))
    else:
        raw = load_seed_rows()
    latest: dict[str, dict] = {}
    for row in sorted(raw, key=lambda item: str(item.get("captured_at") or "")):
        video_id = str(row.get("video_id") or "").strip()
        if video_id:
            latest[video_id] = row
    now = now or datetime.now(timezone.utc)
    return refresh_velocity(list(latest.values()), now)


def usable_rows(rows: list[dict], *, min_duration_s: int = MIN_DURATION_S,
                min_age_days: float = MIN_AGE_DAYS,
                aliases: dict[str, str] | None = None) -> list[dict]:
    """Keep Recognised-Pro POV videos with a stable views/day measurement."""
    aliases = recognised_aliases() if aliases is None else aliases
    out: list[dict] = []
    for row in rows:
        published = str(row.get("published_at") or "").strip()
        player = canonical_player(row.get("primary_player") or "", aliases)
        if not published or not player:
            continue
        velocity = _num(row.get("views_per_day"))
        age = _num(row.get("age_days"))
        if velocity is None or velocity <= 0 or age is None or age < min_age_days:
            continue
        if (_num(row.get("duration_seconds")) or 0.0) < min_duration_s:
            continue
        kept = dict(row)
        kept["player"] = player
        kept["published_at"] = published
        kept["age_days_num"] = age
        kept["views_per_day_num"] = velocity
        kept["views_num"] = _num(row.get("views")) or 0.0
        out.append(kept)
    return out


def attach_performance(rows: list[dict]) -> list[dict]:
    """``performance_index`` = views/day over its channel's median views/day.

    The channel baseline is computed from the rows handed in, so a held-out
    fold is normalised against itself and never borrows the fit fold's level.
    """
    from collections import defaultdict

    by_channel: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_channel[str(row.get("channel") or "")].append(row)
    scored: list[dict] = []
    for channel, items in by_channel.items():
        velocities = [item["views_per_day_num"] for item in items]
        baseline = median(velocities) or 1.0
        channel_threshold = _quantile(velocities, 0.75)
        for item in items:
            row = dict(item)
            row["performance_index"] = item["views_per_day_num"] / baseline
            row["channel_top_quartile"] = item["views_per_day_num"] >= channel_threshold
            scored.append(row)
    pooled = [row["performance_index"] for row in scored]
    pooled_threshold = _quantile(pooled, 0.75)
    for row in scored:
        row["pooled_top_quartile"] = row["performance_index"] >= pooled_threshold
    return scored


def predict_star(index: dict[str, float], row: dict) -> float:
    """Production lookup: casefolded key, neutral 1.0 when the player is absent."""
    return float(index.get(str(row["player"]).casefold(), NEUTRAL_STAR))


def _published_before(row: dict, at: datetime) -> bool:
    """True when a row's publish stamp is strictly before ``at``."""
    raw = str(row.get("published_at") or "").strip()
    if not raw:
        return False
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp < at


def fit_index_at(rows: list[dict], at: datetime, *,
                 window_days: int = WINDOW_DAYS,
                 recent_days: int = RECENT_DAYS,
                 aliases: dict[str, str] | None = None) -> tuple[dict[str, float], dict[str, dict]]:
    """Rebuild the shipped index using only rows published before ``at``.

    Future rows are dropped here rather than by the caller: ``in_window`` only
    trims the *old* side, so without this guard passing the full scrape would
    leak held-out videos straight into the star.
    """
    aliases = recognised_aliases() if aliases is None else aliases
    canonical = []
    for row in rows:
        if not _published_before(row, at):
            continue
        player = canonical_player(row.get("primary_player") or "", aliases)
        if not player:
            continue
        kept = dict(row)
        kept["primary_player"] = player
        # ``analyze_rows`` groups on the full scrape's columns; fill the two it
        # reads unconditionally so the evaluator works on a bare row too.
        if not kept.get("publish_hour_utc"):
            kept["publish_hour_utc"] = _publish_hour(kept.get("published_at"))
        kept.setdefault("title", "")
        kept.setdefault("url", "")
        canonical.append(kept)
    long_report = analyze_rows(in_window(canonical, at, window_days), source="star-eval-fit")
    recent_report = analyze_rows(in_window(canonical, at, recent_days), source="star-eval-recent")
    return build_index(long_report, recent_report, aliases)


def _publish_hour(published) -> str:
    """UTC publish hour of an ISO stamp (``analyze_rows`` groups on h00/h06/...)."""
    try:
        stamp = datetime.fromisoformat(str(published).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return "0"
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return str(stamp.astimezone(timezone.utc).hour)


def load_shipped_index(path: Path | None = None) -> tuple[dict[str, float], dict[str, dict]]:
    """Production artifact: casefolded index plus its per-player detail table."""
    payload = json.loads(Path(path or DEMAND_PATH).read_text(encoding="utf-8"))
    raw = payload.get("index", payload) or {}
    index = {str(key).casefold(): float(value) for key, value in raw.items()}
    return index, payload.get("players") or {}


def _sorted_rows(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda row: str(row["published_at"]))


def split_rows(rows: list[dict], cutoff: datetime) -> tuple[list[dict], list[dict]]:
    """Strict time split: fit rows are published before the cutoff, held-out at/after."""
    fit, holdout = [], []
    for row in _sorted_rows(rows):
        (fit if _published_before(row, cutoff) else holdout).append(row)
    return fit, holdout


def correlation_block(rows: list[dict], predict) -> dict:
    """Rank/linear correlation of a per-video prediction with log performance."""
    pairs = [
        (float(predict(row)), math.log(row["performance_index"]))
        for row in rows
        if row["performance_index"] > 0
    ]
    if len(pairs) < 3:
        return {"n": len(pairs), "spearman": None, "pearson": None}
    xs = [x for x, _ in pairs]
    ys = [y for _, y in pairs]
    return {"n": len(pairs), "spearman": round(spearman(xs, ys), 4), "pearson": _pearson(pairs)}


def star_predictor(index: dict[str, float]):
    return lambda row: predict_star(index, row)


def raw_signal_predictor(details: dict[str, dict]):
    """Unclipped fit-fold median performance index, keyed by casefolded player.

    The star clips to 1.08-1.80, caps thin samples at 1.35 and blends a 30-day
    overlay in. Grading this raw median next to the star shows whether those
    transforms cost ranking power or buy stability.
    """
    table = {
        str(player).casefold(): float(info.get("median_performance_index") or 0.0)
        for player, info in details.items()
    }
    return lambda row: table.get(str(row["player"]).casefold(), NEUTRAL_STAR)


def gate_block(rows: list[dict], index: dict[str, float], *,
               threshold: float = STAR_FLOOR, target: str = "channel_top_quartile") -> dict:
    """Confusion metrics for ``star >= threshold`` against a top-quartile target."""
    flagged = hits = successes = 0
    for row in rows:
        star = predict_star(index, row)
        good = bool(row.get(target))
        successes += int(good)
        if star >= threshold:
            flagged += 1
            hits += int(good)
    n = len(rows)
    base_rate = successes / n if n else 0.0
    precision = hits / flagged if flagged else 0.0
    recall = hits / successes if successes else 0.0
    lift = precision / base_rate if base_rate else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall)
        else 0.0
    )
    return {
        "threshold": threshold,
        "videos": n,
        "flagged": flagged,
        "flagged_share": round(flagged / n, 4) if n else 0.0,
        "base_rate": round(base_rate, 4),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "lift": round(lift, 3),
    }


def gate_sweep(rows: list[dict], index: dict[str, float], *,
               thresholds=SWEEP_THRESHOLDS, target: str = "channel_top_quartile") -> list[dict]:
    return [gate_block(rows, index, threshold=value, target=target)
            for value in thresholds]


def band_table(rows: list[dict], index: dict[str, float], *,
               target: str = "channel_top_quartile") -> list[dict]:
    table = []
    for lower, upper, label in BANDS:
        members = []
        for row in rows:
            star = predict_star(index, row)
            if lower is not None and star < lower:
                continue
            if upper is not None and star >= upper:
                continue
            members.append(row)
        if not members:
            table.append({"band": label, "videos": 0})
            continue
        velocities = [row["performance_index"] for row in members]
        table.append({
            "band": label,
            "videos": len(members),
            "share": round(len(members) / len(rows), 4) if rows else 0.0,
            "median_performance_index": round(median(velocities), 3),
            "top_quartile_rate": round(
                sum(row[target] for row in members) / len(members), 3
            ),
        })
    return table


def player_table(rows: list[dict], index: dict[str, float],
                 details: dict[str, dict] | None = None,
                 *, minimum: int = 2) -> list[dict]:
    """Per-player held-out performance next to the star that predicted it."""
    from collections import defaultdict

    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[row["player"]].append(row)
    details = details or {}
    table = []
    for player, items in groups.items():
        if len(items) < minimum:
            continue
        key = player.casefold()
        info = details.get(player) or details.get(key) or {}
        velocities = [row["performance_index"] for row in items]
        table.append({
            "player": player,
            "star": index.get(key),
            "fit_videos": info.get("videos"),
            "heldout_videos": len(items),
            "heldout_median_performance_index": round(median(velocities), 3),
            "heldout_top_quartile_rate": round(
                sum(row["channel_top_quartile"] for row in items) / len(items), 3
            ),
            "heldout_median_views_per_day": round(
                median([row["views_per_day_num"] for row in items]), 1
            ),
        })
    table.sort(key=lambda item: (-(item["star"] or NEUTRAL_STAR), item["player"]))
    return table


def precision_at_k(rows: list[dict], index: dict[str, float],
                   *, ks=(10, 20, 50), target: str = "channel_top_quartile") -> list[dict]:
    """Top-K-by-star quality at the daily-selection scale.

    Ties break on player nick (never on the target) so the number stays honest.
    """
    ranked = sorted(rows, key=lambda row: (-predict_star(index, row), row["player"].casefold()))
    base_rate = (
        sum(row[target] for row in rows) / len(rows) if rows else 0.0
    )
    out = []
    for k in ks:
        if k > len(ranked):
            continue
        head = ranked[:k]
        hit = sum(row[target] for row in head) / k
        out.append({
            "k": k,
            "precision": round(hit, 4),
            "base_rate": round(base_rate, 4),
            "lift": round(hit / base_rate, 3) if base_rate else 0.0,
        })
    return out


def permutation_p(xs: list[float], ys: list[float], *, permutations: int = 2000,
                  seed: int = 0) -> float | None:
    """Two-sided permutation p-value for a rank correlation.

    With ~12 rated players the spearman number alone is unreadable; this says
    whether it could plausibly come from a random re-labelling of the stars.
    """
    import random

    observed = spearman(xs, ys)
    if observed == 0.0:
        return 1.0
    rng = random.Random(seed)
    shuffled = list(xs)
    hits = 0
    for _ in range(permutations):
        rng.shuffle(shuffled)
        if abs(spearman(shuffled, ys)) >= abs(observed):
            hits += 1
    return round((hits + 1) / (permutations + 1), 4)


def player_level_block(players: list[dict], *, floor: float = STAR_FLOOR,
                       permutations: int = 2000) -> dict:
    """The star is a per-player constant: grade it where it actually varies.

    A single star cannot explain a player's own video-to-video spread, so the
    player-level rank correlation and the floor's split are the headline.
    """
    rated = [item for item in players if item["star"] is not None]
    gated = [item for item in rated if float(item["star"]) >= floor]
    below = [item for item in rated if float(item["star"]) < floor]
    unrated = [item for item in players if item["star"] is None]

    def summary(items: list[dict]) -> dict:
        if not items:
            return {"players": 0}
        return {
            "players": len(items),
            "median_performance_index": round(
                median([item["heldout_median_performance_index"] for item in items]), 3
            ),
            "median_top_quartile_rate": round(
                median([item["heldout_top_quartile_rate"] for item in items]), 3
            ),
        }

    block = {
        "rated_players": len(rated),
        "rated_players_5plus_heldout": sum(
            1 for item in rated if item["heldout_videos"] >= 5
        ),
        "unrated_players": len(unrated),
        "spearman_star_vs_median_performance": None,
        "spearman_p_value": None,
        "at_or_above_floor": summary(gated),
        "below_floor": summary(below),
        "unrated": summary(unrated),
    }
    if len(rated) >= 5:
        xs = [float(item["star"]) for item in rated]
        ys = [item["heldout_median_performance_index"] for item in rated]
        block["spearman_star_vs_median_performance"] = round(spearman(xs, ys), 4)
        block["spearman_p_value"] = permutation_p(xs, ys, permutations=permutations)
    if gated and below:
        block["floor_lift"] = round(
            block["at_or_above_floor"]["median_performance_index"]
            / (block["below_floor"]["median_performance_index"] or 1.0),
            3,
        )
    return block


def evaluate(rows: list[dict], *, mode: str = "time-split",
             eval_days: int = 90, window_days: int = WINDOW_DAYS,
             recent_days: int = RECENT_DAYS, floor: float = STAR_FLOOR,
             now: datetime | None = None,
             aliases: dict[str, str] | None = None) -> dict:
    """Run one eval protocol over prepared history rows."""
    now = now or datetime.now(timezone.utc)
    aliases = recognised_aliases() if aliases is None else aliases
    prepared = usable_rows(rows, aliases=aliases)

    if mode == "in-sample":
        index, details = load_shipped_index()
        scored = attach_performance(prepared)
        cutoff = None
        fit_rows: list[dict] = []
    else:
        if eval_days < MIN_EVAL_DAYS:
            raise ValueError(f"--eval-days must be >= {MIN_EVAL_DAYS}")
        from datetime import timedelta

        cutoff = now - timedelta(days=eval_days)
        fit_rows, holdout = split_rows(prepared, cutoff)
        if len(holdout) < MIN_HOLDOUT_VIDEOS:
            raise ValueError(
                f"only {len(holdout)} held-out videos; lower --eval-days"
            )
        if len(fit_rows) < MIN_FIT_VIDEOS:
            raise ValueError(
                f"only {len(fit_rows)} fit videos before the cutoff; "
                "lower --eval-days"
            )
        index, details = fit_index_at(
            fit_rows, cutoff, window_days=window_days, recent_days=recent_days,
            aliases=aliases,
        )
        scored = attach_performance(holdout)
        newest_fit = max((row["published_at"] for row in fit_rows), default="")
        oldest_holdout = min(row["published_at"] for row in scored)
        assert not newest_fit or newest_fit < oldest_holdout, "time split leaked"

    stars = [predict_star(index, row) for row in scored]
    covered = [row for row in scored if row["player"].casefold() in index]
    result = {
        "generated_at": now.isoformat(),
        "mode": mode,
        "protocol": {
            "cutoff": cutoff.isoformat() if cutoff else None,
            "eval_days": eval_days if cutoff else None,
            "window_days": window_days,
            "recent_days": recent_days,
            "star_floor": floor,
            "target": "channel-relative top quartile of views/day",
            "neutral_star": NEUTRAL_STAR,
            "circular": mode == "in-sample",
        },
        "fit": {
            "videos": len(fit_rows),
            "players": len(index),
            "stars_at_or_above_floor": sum(1 for value in index.values() if value >= floor),
            "index_median": round(median(list(index.values())), 3) if index else None,
        },
        "holdout": {
            "videos": len(scored),
            "players": len({row["player"].casefold() for row in scored}),
            "covered_videos": len(covered),
            "coverage": round(len(covered) / len(scored), 4) if scored else 0.0,
            "channel_top_quartile_rate": round(
                sum(row["channel_top_quartile"] for row in scored) / len(scored), 4
            ) if scored else 0.0,
            "median_views_per_day": round(
                median([row["views_per_day_num"] for row in scored]), 1
            ) if scored else None,
        },
        "correlations": {
            "star_all": correlation_block(scored, star_predictor(index)),
            "star_covered_only": correlation_block(covered, star_predictor(index)),
            "raw_signal_all": correlation_block(scored, raw_signal_predictor(details)),
            "subscribers_baseline": _subscriber_block(scored),
        },
        "gate": gate_block(scored, index, threshold=floor),
        "gate_pooled_target": gate_block(
            scored, index, threshold=floor, target="pooled_top_quartile"
        ),
        "precision_at_k": precision_at_k(scored, index),
        "bands": band_table(scored, index),
        "sweep": gate_sweep(scored, index),
        "players": player_table(scored, index, details),
    }
    result["player_level"] = player_level_block(result["players"], floor=floor)
    result["caveats"] = _caveats(result)
    result["stars_used"] = {
        "distinct": len(set(stars)),
        "median": round(median(stars), 3) if stars else None,
        "share_neutral": round(
            sum(1 for value in stars if value == NEUTRAL_STAR) / len(stars), 4
        ) if stars else 0.0,
    }
    return result


def _subscriber_block(rows: list[dict]) -> dict:
    """Sanity baseline: raw channel scale should not beat the star."""
    pairs = []
    for row in rows:
        subs = _num(row.get("subscribers"))
        if subs and subs > 0 and row["performance_index"] > 0:
            pairs.append((float(subs), math.log(row["performance_index"])))
    if len(pairs) < 3:
        return {"n": len(pairs), "spearman": None}
    return {"n": len(pairs), "spearman": round(spearman(
        [x for x, _ in pairs], [y for _, y in pairs]
    ), 4)}


def format_report(result: dict) -> str:
    lines: list[str] = []
    add = lines.append
    protocol = result["protocol"]
    add(f"star eval - mode={result['mode']}"
        + (" (CIRCULAR: index is fitted on the same videos)" if protocol["circular"] else ""))
    if protocol["cutoff"]:
        add(f"cutoff {protocol['cutoff']}  (eval_days={protocol['eval_days']})")
    add(f"star floor {protocol['star_floor']}   target: {protocol['target']}")
    add("")
    fit, holdout = result["fit"], result["holdout"]
    if protocol["circular"]:
        add(f"shipped index: {fit['players']:>4} players, "
            f"{fit['stars_at_or_above_floor']} stars >= {protocol['star_floor']}"
            + (f", index median {fit['index_median']}" if fit["index_median"] else ""))
    else:
        add(f"fit window : {fit['videos']:>5} videos, {fit['players']:>4} players, "
            f"{fit['stars_at_or_above_floor']} stars >= {protocol['star_floor']}"
            + (f", index median {fit['index_median']}" if fit["index_median"] else ""))
    add(f"held-out   : {holdout['videos']:>5} videos, {holdout['players']:>4} players, "
        f"coverage {holdout['coverage']:.1%}, "
        f"median {holdout['median_views_per_day']} views/day")
    add("")
    corr = result["correlations"]
    rows_out = (("star (all held-out)", "star_all"),
                ("star (covered only)", "star_covered_only"),
                ("raw median (unclipped)", "raw_signal_all"),
                ("subscribers baseline", "subscribers_baseline"))
    for label, key in rows_out:
        block = corr.get(key)
        if not block:
            continue
        add(f"  {label:<22} n={block['n']:<5} spearman={_fmt(block['spearman'])}"
            + (f"  pearson={_fmt(block.get('pearson'))}" if block.get("pearson") is not None else ""))
    add("")
    gate = result["gate"]
    add(f"gate star >= {gate['threshold']}: flagged {gate['flagged']} "
        f"({gate['flagged_share']:.1%}), precision {gate['precision']:.3f}, "
        f"recall {gate['recall']:.3f}, lift {gate['lift']:.2f}x "
        f"(base {gate['base_rate']:.1%})")
    for step in result.get("precision_at_k") or []:
        add(f"  precision@{step['k']:<3} {step['precision']:.3f}  lift {step['lift']:.2f}x")
    add("")
    level = result["player_level"]
    add(f"player level: {level['rated_players']} rated "
        f"({level['rated_players_5plus_heldout']} with >=5 held-out videos) / "
        f"{level['unrated_players']} unrated")
    add(f"  spearman(star vs held-out median)={_fmt(level['spearman_star_vs_median_performance'])}"
        + (f"  permutation p={level['spearman_p_value']:.3f}"
           if level.get("spearman_p_value") is not None else ""))
    for label, key in (("star >= floor", "at_or_above_floor"),
                       ("star <  floor", "below_floor"),
                       ("no star", "unrated")):
        block = level[key]
        if not block.get("players"):
            continue
        add(f"  {label:<14} {block['players']:>3} players, median perf "
            f"{block['median_performance_index']:.3f}, median top-quartile "
            f"{block['median_top_quartile_rate']:.1%}")
    if level.get("floor_lift") is not None:
        add(f"  floor lift at player level: {level['floor_lift']:.2f}x")
    add("")
    add("  band                 videos   share  median perf  top-quartile")
    for band in result["bands"]:
        if not band.get("videos"):
            continue
        add(f"  {band['band']:<20} {band['videos']:>6}  {band['share']:>6.1%}  "
            f"{band['median_performance_index']:>11}  {band['top_quartile_rate']:>12.1%}")
    add("")
    add("  threshold  flagged   prec   recall  lift")
    for step in result["sweep"]:
        add(f"  {step['threshold']:>9.2f} {step['flagged']:>7}  "
            f"{step['precision']:.3f}  {step['recall']:.3f}  {step['lift']:.2f}x")
    ranking = result["player_level"]
    lines.append("")
    lines.append(f"  player band table covers {ranking['rated_players']} rated players "
                 f"(see player_level in the JSON for the floor split)")
    lines.append("")
    lines.append("  player        star  fit n  heldout  median perf  top-quartile")
    for item in result["players"][:20]:
        star = item["star"]
        star_text = "  -" if star is None else f"{star:.2f}"
        add(f"  {item['player']:<13} {star_text:>4}  "
            f"{item['fit_videos'] if item['fit_videos'] is not None else '-':>5}  "
            f"{item['heldout_videos']:>7}  "
            f"{item['heldout_median_performance_index']:>11}  "
            f"{item['heldout_top_quartile_rate']:>12.1%}")
    return "\n".join(lines)


def _fmt(value) -> str:
    return "n/a" if value is None else f"{value:+.3f}"


def _caveats(result: dict) -> list[str]:
    notes = [
        "Ground truth is competitor POV view velocity: a star is a per-player "
        "constant, so video-level correlation mostly measures channel/mix noise.",
        "performance_index is normalised inside the fold, so a channel's median "
        "POV can never be called a hit (base rate is pinned near 25%).",
        "Held-out velocity is measured at report time; newer videos are noisier "
        "even after the views/day normalisation.",
        "Coverage counts only players the index accepted (>= 8 long-window or "
        ">= 3 recent videos); unrated players score the neutral 1.0.",
        "Only ~13 players survive the index's sample-size floors per split, so "
        "the player-level spearman is high-variance: read the permutation p-value.",
    ]
    if result["protocol"]["circular"]:
        notes.insert(0, "In-sample mode grades the shipped index on the same "
                        "videos it was fitted from: circular, use for reference only.")
    if (result.get("history") or {}).get("videos"):
        notes.append("Faces of the split are uneven: the scrape skews recent, "
                     "so the fit fold is much smaller than the held-out fold.")
    return notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--history", type=Path, default=None,
                        help="single POV-market CSV (default: production seed union)")
    parser.add_argument("--mode", choices=("time-split", "in-sample"), default="time-split",
                        help="time-split rebuilds the index at the cutoff (default)")
    parser.add_argument("--eval-days", type=int, default=90,
                        help="held-out window length in days (default 90)")
    parser.add_argument("--window-days", type=int, default=WINDOW_DAYS)
    parser.add_argument("--recent-days", type=int, default=RECENT_DAYS)
    parser.add_argument("--floor", type=float, default=STAR_FLOOR,
                        help=f"star gate to grade (default {STAR_FLOOR})")
    parser.add_argument("--out", type=Path, default=OUT_DEFAULT,
                        help=f"JSON report path (default {OUT_DEFAULT.relative_to(ROOT)})")
    parser.add_argument("--sweep-days", default="",
                        help="comma-separated --eval-days values to compare, e.g. 60,90,120")
    parser.add_argument("--no-write", action="store_true", help="print only, write no JSON")
    args = parser.parse_args(argv)

    rows = load_videos(history_path=args.history)
    try:
        result = evaluate(
            rows,
            mode=args.mode,
            eval_days=args.eval_days,
            window_days=args.window_days,
            recent_days=args.recent_days,
            floor=args.floor,
        )
    except ValueError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    result["history"] = {
        "videos": len(rows),
        "source": str(args.history) if args.history else "production seed union",
    }
    result["caveats"] = _caveats(result)
    print(format_report(result))
    if args.sweep_days:
        days = [int(part) for part in args.sweep_days.split(",") if part.strip()]
        comparisons = []
        print("\ncutoff sweep (time-split)")
        print("  days  fit  holdout  coverage  video rho  player rho  gate lift  prec@20")
        for day in days:
            try:
                block = evaluate(
                    rows, mode="time-split", eval_days=day,
                    window_days=args.window_days, recent_days=args.recent_days,
                    floor=args.floor,
                )
            except ValueError as exc:
                print(f"  {day:>4}  skipped: {exc}")
                continue
            top20 = next((step for step in block["precision_at_k"] if step["k"] == 20), {})
            comparisons.append({
                "eval_days": day,
                "fit_videos": block["fit"]["videos"],
                "holdout_videos": block["holdout"]["videos"],
                "coverage": block["holdout"]["coverage"],
                "video_spearman": block["correlations"]["star_all"]["spearman"],
                "player_spearman": block["player_level"][
                    "spearman_star_vs_median_performance"],
                "gate_lift": block["gate"]["lift"],
                "precision_at_20": top20.get("precision"),
            })
            print(f"  {day:>4}  {block['fit']['videos']:>3}  "
                  f"{block['holdout']['videos']:>7}  "
                  f"{block['holdout']['coverage']:>8.1%}  "
                  f"{_fmt(block['correlations']['star_all']['spearman']):>9}  "
                  f"{_fmt(block['player_level']['spearman_star_vs_median_performance']):>10}  "
                  f"{block['gate']['lift']:>9.2f}  "
                  f"{top20.get('precision') if top20 else '-':>7}")
        result["cutoff_sweep"] = comparisons
    if not args.no_write:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
