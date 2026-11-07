"""Coverage report: what was actually collected, and what could not be.

The report is the honesty mechanism. It prints each platform's declared access
status next to the observed row counts, so "zero Instagram play counts" cannot be
mistaken for "Instagram had no plays".
"""

from __future__ import annotations

import json
from pathlib import Path

from cs2archive.shorts.social.config import SweepConfig, load_config
from cs2archive.shorts.social.features import summarize_features
from cs2archive.shorts.social.models import METRIC_FIELDS
from cs2archive.shorts.social.platforms import KNOWN_PLATFORMS, adapter_for
from cs2archive.shorts.social.store import SocialStore

TARGET_METRIC = {
    "youtube": "views",
    "allstar": "views",
    "instagram": "likes",  # documented proxy: Business Discovery exposes no plays
    "tiktok": None,
    "reddit": None,
}


def platform_coverage(store: SocialStore, config: SweepConfig, env, platform: str) -> dict:
    pcfg = config.platform(platform)
    try:
        adapter = adapter_for(platform, pcfg)
        declared = adapter.platform_status(pcfg, env)
        status = {"status": declared.status, "reason": declared.reason,
                  "mode": declared.mode, "requirements": declared.requirements}
    except Exception as exc:  # noqa: BLE001 - report must not fail on one adapter
        status = {"status": "unknown", "reason": f"adapter error: {exc}",
                  "mode": "", "requirements": []}

    posts = list(store.conn.execute(
        "SELECT * FROM posts WHERE platform=?", (platform,)))
    snapshots = list(store.conn.execute(
        "SELECT * FROM metric_snapshots WHERE platform=?", (platform,)))
    available = {name: sum(1 for s in snapshots if s[name] is not None)
                 for name in METRIC_FIELDS}
    formats: dict[str, int] = {}
    for post in posts:
        formats[str(post["format_label"] or "unknown")] = \
            formats.get(str(post["format_label"] or "unknown"), 0) + 1
    published = [p["published_at"] for p in posts if p["published_at"]]
    runs = [r for r in store.runs() if r["platform"] == platform]
    errors = [e for e in store.errors() if e["platform"] == platform]
    link_counts = {
        "exact": store.conn.execute(
            "SELECT COUNT(*) FROM clip_identity_links WHERE platform=? AND ambiguous=0 "
            "AND confidence >= 0.8", (platform,)).fetchone()[0],
        "ambiguous": store.conn.execute(
            "SELECT COUNT(*) FROM clip_identity_links WHERE platform=? AND ambiguous=1",
            (platform,)).fetchone()[0],
    }
    linked_ids = {row[0] for row in store.conn.execute(
        "SELECT p.snapshot_id FROM snapshot_provenance p JOIN metric_snapshots s "
        "ON s.snapshot_id = p.snapshot_id WHERE s.platform=?", (platform,))}
    with_raw = sum(1 for s in snapshots
                   if s["raw_response_id"] is not None
                   or s["snapshot_id"] in linked_ids)
    return {
        "platform": platform,
        "declared": status,
        "posts": len(posts),
        "snapshots": len(snapshots),
        "snapshots_with_raw_provenance": with_raw,
        "creators": len({p["creator_name"] for p in posts if p["creator_name"]}),
        "with_published_at": len(published),
        "published_range": [min(published), max(published)] if published else None,
        "with_duration": sum(1 for p in posts if p["duration_seconds"] is not None),
        "formats": formats,
        "metric_non_null": available,
        "links": link_counts,
        "quarantines": store.conn.execute(
            "SELECT COUNT(*) FROM quarantines WHERE platform=?", (platform,)).fetchone()[0],
        "latest_run": dict(runs[0]) if runs else None,
        "error_codes": _error_codes(errors),
        "target_metric": TARGET_METRIC.get(platform),
    }


def _error_codes(errors) -> dict[str, int]:
    out: dict[str, int] = {}
    for error in errors:
        out[error["error_code"]] = out.get(error["error_code"], 0) + 1
    return out


def build_report(config_path: Path, *, env=None) -> dict:
    from cs2archive.shorts.social.collect import env_from_settings

    config = load_config(Path(config_path))
    env = env or env_from_settings()
    store = SocialStore(config.database, secrets=env.secrets())
    try:
        coverage = {name: platform_coverage(store, config, env, name)
                    for name in KNOWN_PLATFORMS}
        for name in config.platforms:
            if name not in coverage:
                coverage[name] = platform_coverage(store, config, env, name)
        groups = _group_summary(store)
        eligibility = _train_eligibility(store, config)
        interpretation = _interpretation(store, groups)
        snapshot_stats = _snapshot_summary(store)
        hypothesis_stats = _hypothesis_summary(store)
        quota = _quota_summary(store)
        linkage_stats = _linkage_section(store)
        analytics_stats = _analytics_section(store)
        ceiling_stats = _ceiling_section(store)
        dossier_stats = _dossiers_section(config)
        return {
            "database": str(config.database),
            "window": {"start": config.window_start, "end": config.window_end},
            "counts": store.counts(),
            "coverage": coverage,
            "features": summarize_features(store),
            "groups": groups,
            "train_eligibility": eligibility,
            "interpretation": interpretation,
            "snapshots": snapshot_stats,
            "hypotheses": hypothesis_stats,
            "quota": quota,
            "linkage": linkage_stats,
            "analytics": analytics_stats,
            "ceiling": ceiling_stats,
            "dossiers": dossier_stats,
            "errors": [dict(e) for e in store.errors()[:50]],
            "runs": [dict(r) for r in store.runs()[:50]],
        }
    finally:
        store.close()


def _group_summary(store: SocialStore) -> dict:
    from cs2archive.shorts.social.identity import assign_groups, identity_coverage

    link_rows: dict[tuple[str, str], list[dict]] = {}
    for link in store.conn.execute("SELECT * FROM clip_identity_links"):
        link_rows.setdefault((link["platform"], link["post_id"]), []).append(dict(link))
    posts = [(row["platform"], row["post_id"])
             for row in store.conn.execute("SELECT platform, post_id FROM posts")]
    groups = assign_groups(posts, link_rows)
    components: dict[str, list[tuple[str, str]]] = {}
    for post, info in groups.items():
        components.setdefault(info["group_id"], []).append(post)
    coverage = identity_coverage(posts, link_rows)
    multi_platform = sum(1 for members in components.values()
                         if len({platform for platform, _ in members}) > 1)
    by_basis: dict[str, int] = {}
    for info in groups.values():
        by_basis[info["basis"]] = by_basis.get(info["basis"], 0) + 1
    return {
        "groups": len(components),
        "multi_platform_groups": multi_platform,
        "largest_group": max((len(m) for m in components.values()), default=0),
        "basis": by_basis,
        "identity_coverage": coverage,
        "unverified_group_posts": coverage["unverified_posts"],
    }


def _train_eligibility(store: SocialStore, config: SweepConfig) -> dict:
    """Eligibility uses the same verified-format filter and split as training."""
    from cs2archive.shorts.social.train import (
        MIN_TEST_ROWS, load_platform_rows, split_rows,
    )

    out: dict[str, dict] = {}
    for platform, metric in TARGET_METRIC.items():
        if metric is None:
            out[platform] = {"target_metric": None, "eligible": False,
                             "reason": "no comparable exposure metric on this path"}
            continue
        rows, stats = load_platform_rows(store, platform)
        verified = [row for row in rows if row.group_basis == "verified"]
        blockers: list[str] = []
        split = split_rows(verified) if verified else {
            "train": [], "test": [], "purged": 0, "train_groups": 0}
        if len(verified) < config.min_train_rows:
            blockers.append(f"{len(verified)} verified-identity short-format rows < "
                            f"{config.min_train_rows} required")
        if len(split["test"]) < MIN_TEST_ROWS:
            blockers.append(f"holdout block would be {len(split['test'])} rows "
                            f"(< {MIN_TEST_ROWS})")
        if split["train_groups"] < 2:
            blockers.append("fewer than 2 verified identity groups in training")
        if len(rows) - len(verified):
            blockers.append(f"{len(rows) - len(verified)} rows excluded as "
                            "unverified identity (scored set is verified-only)")
        out[platform] = {
            "target_metric": metric,
            "rows_usable": len(verified),
            "rows_format_usable": len(rows),
            "rows_required": config.min_train_rows,
            "candidates": stats["candidates"],
            "unverified_format": stats["unverified_format"],
            "format_rejections": stats["format_rejections"],
            "missing_published_at": stats["missing_published_at"],
            "missing_target": stats["missing_target"],
            "quarantined": stats["quarantined"],
            "identity_verified_share": stats.get("verified_identity_share", 0.0),
            "split": {"train": len(split["train"]), "test": len(split["test"]),
                      "purged": split["purged"],
                      "train_groups": split["train_groups"]},
            "eligible": not blockers,
            "blockers": blockers,
        }
    return out


def _snapshot_summary(store: SocialStore) -> dict:
    """Repeated-observation evidence: counts and the real interval between them."""
    from datetime import datetime

    out: dict[str, dict] = {}
    for platform in sorted({row["platform"] for row in store.conn.execute(
            "SELECT DISTINCT platform FROM metric_snapshots")}):
        rows = list(store.conn.execute(
            "SELECT post_id, observed_at FROM metric_snapshots WHERE platform=? "
            "ORDER BY post_id, observed_at", (platform,)))
        per_post: dict[str, list[str]] = {}
        for row in rows:
            per_post.setdefault(row["post_id"], []).append(row["observed_at"])
        repeated = {k: v for k, v in per_post.items() if len(v) > 1}
        gaps = []
        for stamps in repeated.values():
            first = datetime.fromisoformat(stamps[0].replace("Z", "+00:00"))
            last = datetime.fromisoformat(stamps[-1].replace("Z", "+00:00"))
            gaps.append((last - first).total_seconds() / 3600.0)
        gaps.sort()
        median_gap = gaps[len(gaps) // 2] if gaps else None
        out[platform] = {
            "snapshots": len(rows),
            "posts": len(per_post),
            "distinct_observation_times": len({row["observed_at"] for row in rows}),
            "posts_with_multiple_snapshots": len(repeated),
            "median_interval_hours_first_to_last": (round(median_gap, 3)
                                                    if median_gap is not None
                                                    else None),
            "min_interval_hours": round(gaps[0], 3) if gaps else None,
            "max_interval_hours": round(gaps[-1], 3) if gaps else None,
            "observed_range": [rows[0]["observed_at"], rows[-1]["observed_at"]]
            if rows else None,
            "note": "intervals are real measurement spacing, never a 24h growth "
                    "reconstruction",
        }
    return out


def _hypothesis_summary(store: SocialStore) -> dict:
    rows = list(store.conn.execute(
        "SELECT platform, method, ambiguous, COUNT(*) n, "
        "COUNT(DISTINCT post_id) posts FROM clip_identity_links "
        "GROUP BY platform, method, ambiguous"))
    exact = {"links": 0, "posts": 0}
    hypotheses: dict[str, dict] = {}
    for row in rows:
        entry = hypotheses.setdefault(row["platform"], {"rows": 0, "posts": 0,
                                                        "methods": set()})
        if row["method"] == "allstar_clip_id":
            exact["links"] += row["n"]
            exact["posts"] += row["posts"]
            continue
        entry["rows"] += row["n"]
        entry["posts"] += row["posts"]
        entry["methods"].add(row["method"])
    for entry in hypotheses.values():
        entry["methods"] = sorted(entry["methods"])
    unlinked = {}
    for row in store.conn.execute(
            "SELECT platform, COUNT(*) n FROM posts p WHERE NOT EXISTS "
            "(SELECT 1 FROM clip_identity_links l WHERE l.platform=p.platform "
            "AND l.post_id=p.post_id) GROUP BY platform"):
        unlinked[row["platform"]] = row["n"]
    return {
        "exact_links": exact,
        "ambiguous_hypotheses": hypotheses,
        "unlinked_posts": unlinked,
        "why_unmatched": {
            "no_stable_clip_id_in_caption": "platform captions carry no Allstar "
                                            "clip id, so only hypotheses are possible",
            "no_player_or_map_signal": "aggregator channels post without a player "
                                       "identity, so no candidate can be proposed",
            "allstar_clip_not_staged": "Allstar clip has no exact staged alignment "
                                       "(title/kind kill count did not match demo kills)",
        },
        "grouping_rule": "only exact (allstar_clip_id) links group or train; every "
                         "hypothesis stays ambiguous",
    }


def _quota_summary(store: SocialStore) -> dict:
    rows = list(store.conn.execute(
        "SELECT platform, COUNT(*) runs, SUM(units_spent) units FROM collection_runs "
        "GROUP BY platform"))
    discovery = list(store.conn.execute(
        "SELECT run_id, candidates, accepted, units_spent FROM discovery_runs "
        "ORDER BY started_at"))
    return {
        "units_by_platform": {row["platform"]: {"runs": row["runs"],
                                                "units": round(row["units"] or 0, 1)}
                              for row in rows},
        "discovery_runs": [{"run_id": row["run_id"], "candidates": row["candidates"],
                            "accepted": row["accepted"],
                            "units": round(row["units_spent"] or 0, 1)}
                           for row in discovery],
        "note": "YouTube Data API quota units are counted by this tool; the real "
                "per-key daily limit is enforced by Google",
    }


def _linkage_section(store: SocialStore) -> dict:
    from cs2archive.shorts.social.linkage import linkage_summary

    rows = list(store.conn.execute(
        "SELECT status, COUNT(*) n, SUM(post_id IS NOT NULL) with_post "
        "FROM short_linkage GROUP BY status"))
    return {
        "summary": linkage_summary(store),
        "by_status": {row["status"]: {"rows": row["n"],
                                      "with_post_id": row["with_post"] or 0}
                      for row in rows},
        "reasons": _top_reasons(store, "short_linkage", "reason"),
    }


def _top_reasons(store: SocialStore, table: str, column: str, limit: int = 6) -> dict:
    rows = list(store.conn.execute(
        f"SELECT {column} AS reason, COUNT(*) n FROM {table} "
        f"WHERE {column} IS NOT NULL GROUP BY {column} ORDER BY n DESC LIMIT ?",
        (limit,)))
    return {row["reason"]: row["n"] for row in rows}


def _analytics_section(store: SocialStore) -> dict:
    from cs2archive.shorts.social.analytics import analytics_status

    status = analytics_status()
    # Scope alone is not proof: the most recent ingestion outcome wins, so a
    # revoked/expired credential cannot read as "available".
    last_error = store.conn.execute(
        "SELECT error_code, message, occurred_at FROM collection_errors "
        "WHERE platform='youtube' AND error_code LIKE 'analytics_%' "
        "ORDER BY error_id DESC LIMIT 1").fetchone()
    available = status["available"]
    reason = status["reason"]
    if last_error is not None:
        available = False
        reason = f"{last_error['message']} (last attempt {last_error['occurred_at']})"
    counts = store.analytics_counts()
    windows = list(store.conn.execute(
        "SELECT window_start, window_end, COUNT(*) n FROM analytics_snapshots "
        "GROUP BY window_start, window_end ORDER BY window_start"))
    return {
        "available": available,
        "reason": reason,
        "token_scoped": status["available"],
        "token_path": status["token_path"],
        "counts": counts,
        "windows": [dict(row) for row in windows],
        "note": "owner-only analytics are a separate metric family; they are never "
                "merged into the public views target",
    }


def _ceiling_section(store: SocialStore) -> dict:
    from cs2archive.shorts.social.ceiling import ceiling_report

    try:
        return ceiling_report(store)
    except Exception as exc:  # noqa: BLE001 - the ceiling must never break the report
        return {"error": f"{type(exc).__name__}: {exc}"}


def _dossiers_section(config) -> dict:
    """Aggregate dossier availability from the on-disk index (read-only)."""
    root = Path(config.foundation_dir) / "dossiers"
    index_path = root / "INDEX.json"
    if not index_path.is_file():
        return {"present": False, "root": str(root),
                "command": "python -m cs2archive.shorts.social_sweep dossiers "
                           "--config sources.json"}
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"present": False, "root": str(root), "error": str(exc)}
    index["present"] = True
    index["sample"] = str(next(iter(sorted(root.glob("*.txt"))), ""))
    return index


def _interpretation(store: SocialStore, groups: dict) -> dict:
    gameplay: dict[str, int] = {}
    published: dict[str, dict] = {}
    for row in store.conn.execute(
            "SELECT platform, COUNT(*) AS n, "
            "SUM(CASE WHEN published_at IS NOT NULL THEN 1 ELSE 0 END) AS pub "
            "FROM posts GROUP BY platform"):
        published[row["platform"]] = {"posts": row["n"], "with_published_at": row["pub"]}
    for row in store.conn.execute(
            "SELECT p.platform, COUNT(DISTINCT p.post_id) AS n "
            "FROM posts p JOIN post_features f "
            "ON f.platform=p.platform AND f.post_id=p.post_id "
            "WHERE f.feature='kills' AND f.value_num IS NOT NULL GROUP BY p.platform"):
        gameplay[row["platform"]] = row["n"]
    statements = []
    hypothesis_posts = sum(
        entry["posts"] for entry in
        (_hypothesis_summary(store).get("ambiguous_hypotheses") or {}).values())
    if groups.get("multi_platform_groups", 0) == 0:
        statements.append(
            "No cross-platform same-gameplay groups exist in this store: the "
            "dataset cannot yet explain cross-platform gameplay performance.")
        statements.append(
            f"{hypothesis_posts} posts carry ambiguous cross-platform hypotheses "
            "(player/map/kills from the caption), which are deliberately excluded "
            "from grouping and training: no caption carries a stable clip id.")
    if not gameplay:
        statements.append("No post has measured gameplay features.")
    else:
        statements.append(
            "Measured gameplay exists only for "
            + ", ".join(f"{name} ({count})" for name, count in gameplay.items())
            + "; platform coverage differs, so cross-platform comparison is not "
              "possible from these rows.")
    if published.get("allstar", {}).get("with_published_at", 0) == 0 and             published.get("allstar", {}).get("posts", 0):
        statements.append(
            "Allstar clips have no verified publication time, so they cannot enter "
            "a chronological holdout.")
    statements.append(
        "No experimental model result here is a claim about what makes a clip "
        "succeed; the production picker is unchanged.")
    return {
        "cross_platform_groups": groups.get("multi_platform_groups", 0),
        "gameplay_measured_posts": gameplay,
        "published_at_coverage": published,
        "can_explain_cross_platform_gameplay": False,
        "statements": statements,
    }


def format_report(report: dict) -> str:
    lines = ["CS2 Shorts social-evidence coverage", "=" * 40,
             f"database: {report['database']}",
             f"window:   {report['window']['start']} -> {report['window']['end']}"]
    counts = report["counts"]
    lines.append(
        f"rows:     posts={counts['posts']} snapshots={counts['snapshots']} "
        f"links={counts['links']} features={counts['features']} "
        f"quarantines={counts['quarantines']} runs={counts['runs']}")
    lines.append("")
    for name, cov in report["coverage"].items():
        declared = cov["declared"]
        lines.append(f"[{name}] access={declared['status']} mode={declared['mode']}")
        lines.append(f"  reason: {declared['reason']}")
        lines.append(
            f"  posts={cov['posts']} snapshots={cov['snapshots']} "
            f"(raw-linked {cov['snapshots_with_raw_provenance']}) "
            f"creators={cov['creators']} published_at={cov['with_published_at']} "
            f"duration={cov['with_duration']} target={cov['target_metric']}")
        if cov["formats"]:
            lines.append(f"  formats: {cov['formats']}")
        if cov["metric_non_null"]:
            lines.append(f"  metric non-null: {cov['metric_non_null']}")
        if cov["links"]:
            lines.append(f"  links: {cov['links']}")
        if cov["error_codes"]:
            lines.append(f"  errors: {cov['error_codes']}")
        if declared["requirements"]:
            lines.append(f"  needs: {'; '.join(declared['requirements'])}")
        lines.append("")
    lines.append(f"groups: {report['groups']}")
    lines.append("")
    snapshot_stats = report.get("snapshots") or {}
    if snapshot_stats:
        lines.append("snapshot intervals (real measurement spacing):")
        for name, entry in snapshot_stats.items():
            lines.append(
                f"  {name:10s} snapshots={entry['snapshots']:5d} "
                f"posts={entry['posts']:5d} "
                f"repeated={entry['posts_with_multiple_snapshots']:5d} "
                f"median_gap_hours="
                f"{entry['median_interval_hours_first_to_last']}")
        lines.append("")
    hypotheses = report.get("hypotheses") or {}
    if hypotheses:
        lines.append(f"identity: exact_links={hypotheses['exact_links']}")
        for name, entry in (hypotheses.get("ambiguous_hypotheses") or {}).items():
            lines.append(f"  {name:10s} ambiguous hypothesis rows={entry['rows']} "
                         f"methods={entry['methods']}")
        lines.append(f"  unlinked posts: {hypotheses.get('unlinked_posts')}")
        lines.append(f"  rule: {hypotheses.get('grouping_rule')}")
        lines.append("")
    quota = report.get("quota") or {}
    if quota:
        lines.append(f"quota: {quota.get('units_by_platform')}")
        lines.append(f"  discovery runs: {quota.get('discovery_runs')}")
        lines.append("")
    linkage = report.get("linkage") or {}
    if linkage:
        lines.append("short → demo linkage:")
        for status, data in (linkage.get("by_status") or {}).items():
            lines.append(f"  {status:14s} rows={data['rows']:5d} "
                         f"with_post_id={data['with_post_id']}")
        for reason, count in (linkage.get("reasons") or {}).items():
            lines.append(f"    reason[{count}] {reason}")
        lines.append("")
    analytics = report.get("analytics") or {}
    if analytics:
        state = "available" if analytics.get("available") else "unavailable"
        lines.append(f"own-channel analytics: {state} "
                     f"{analytics.get('reason') or ''}".rstrip())
        if analytics.get("counts"):
            lines.append(f"  rows: {analytics['counts']}")
        if analytics.get("windows"):
            lines.append(f"  windows: {analytics['windows']}")
        lines.append("")
    dossiers = report.get("dossiers") or {}
    if dossiers.get("present"):
        lines.append("moment dossiers:")
        lines.append(f"  moments={dossiers.get('availability', {}).get('dossiers')} "
                     f"built={dossiers.get('built')} "
                     f"skipped={dossiers.get('skipped_current')} "
                     f"failed={dossiers.get('failed')}")
        lines.append(f"  by_source: {dossiers.get('by_source')}")
        lines.append(f"  feature presence: "
                     f"{dossiers.get('availability', {}).get('present_share')}")
        lines.append(f"  sample: {dossiers.get('sample')}")
        lines.append("")
    ceiling = report.get("ceiling") or {}
    if ceiling and "error" not in ceiling:
        summary = ceiling.get("competitor_summary") or {}
        lines.append("demo-linkage ceiling:")
        for name, data in (ceiling.get("per_platform") or {}).items():
            lines.append(f"  {name:10s} exact={data['linked_exact']:5d} "
                         f"title_feasible={data['title_feasible']:5d} "
                         f"evidence_feasible={data['evidence_feasible_local_demo']:5d} "
                         f"unlinkable={data['unlinkable']:5d} "
                         f"({data['unlinkable_share']})")
        lines.append(f"  competitors unlinkable share: {summary.get('unlinkable_share')} "
                     f"of {summary.get('posts')}")
        lines.append("")
    lines.append("training eligibility:")
    for name, entry in report.get("train_eligibility", {}).items():
        if entry.get("target_metric") is None:
            lines.append(f"  {name:10s} eligible=False ({entry.get('reason')})")
            continue
        lines.append(f"  {name:10s} eligible={entry['eligible']} "
                     f"rows={entry.get('rows_usable')}/{entry.get('rows_required')} "
                     f"target={entry.get('target_metric')} "
                     f"identity={entry.get('identity_verified_share')}")
        for blocker in entry.get("blockers") or []:
            lines.append(f"             blocked: {blocker}")
        if entry.get("format_rejections"):
            lines.append(f"             format rejections: {entry['format_rejections']}")
    lines.append("")
    quota = report.get("quota") or {}
    if quota:
        lines.append(f"quota: {quota.get('units_by_platform')}")
    for name, cov in report["coverage"].items():
        missing = [metric for metric, count in cov["metric_non_null"].items()
                   if count == 0]
        if missing:
            lines.append(f"  [{name}] never-exposed metrics: {missing}")
    lines.append("")
    interp = report.get("interpretation") or {}
    if interp:
        lines.append("interpretation:")
        for statement in interp.get("statements") or []:
            lines.append(f"  - {statement}")
        lines.append("")
    lines.append("feature availability (present/missing):")
    for feature, meta in report["features"].items():
        lines.append(f"  {meta['kind']:12s} {feature:26s} {meta['present']:5d}/"
                     f"{meta['present'] + meta['missing']:<5d} "
                     f"{','.join(meta['provenance'])}")
    return "\n".join(lines)


def report_to_json(report: dict) -> str:
    return json.dumps(report, indent=2, sort_keys=True, default=str)
