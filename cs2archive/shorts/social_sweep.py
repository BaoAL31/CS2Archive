"""CLI for the cross-platform Shorts social-evidence sweep.

    python -m cs2archive.shorts.social_sweep collect --config sources.json
    python -m cs2archive.shorts.social_sweep report  --config sources.json
    python -m cs2archive.shorts.social_sweep train   --config sources.json --experimental

Nothing here prints credentials. ``collect`` persists only sanitised raw
responses and parameter names (values of key/token parameters are replaced).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_CONFIG = "sources.json"


def _cmd_collect(args) -> int:
    from cs2archive.shorts.social.collect import run_collection

    result = run_collection(
        Path(args.config), platforms=args.platform or None,
        dry_run=args.dry_run, max_posts=args.max_posts, enrich=not args.no_enrich)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    else:
        for run in result["runs"]:
            print(f"[{run['platform']}] {run['status']} "
                  f"posts={run.get('posts_kept', 0)} "
                  f"snapshots={run.get('snapshots_written', 0)} "
                  f"errors={run.get('errors', 0)}"
                  + (f" | {run['reason']}" if run.get("reason") else ""))
        counts = result["counts"]
        print(f"database={result['database']} posts={counts['posts']} "
              f"snapshots={counts['snapshots']} links={counts['links']}")
        enrichment = result.get("enrichment") or {}
        if enrichment:
            print(f"features={enrichment['features']}")
            print(f"links={enrichment['links']}")
    failed = [run for run in result["runs"] if run["status"] == "failed"]
    return 1 if failed else 0


def _cmd_report(args) -> int:
    from cs2archive.shorts.social.report import build_report, format_report, report_to_json

    report = build_report(Path(args.config))
    text = report_to_json(report) if args.json else format_report(report)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


def _cmd_dossiers(args) -> int:
    import json as _json

    from cs2archive.shorts.social.config import load_config
    from cs2archive.shorts.social.dossier_build import (
        DEFAULT_ROOT, build_all, enrich_state_batch, plan_state_enrichment,
    )
    from cs2archive.shorts.social.store import SocialStore

    config = load_config(Path(args.config))
    root = Path(args.root) if args.root else Path(config.foundation_dir) / "dossiers"
    if args.plan_state:
        todo = plan_state_enrichment(foundation_dir=config.foundation_dir,
                                     limit=args.limit)
        print(_json.dumps({"state_enrichment_candidates": len(todo),
                           "examples": [str(t["clip_dir"]) for t in todo[:5]]},
                          indent=1))
        return 0
    if args.enrich_state:
        stats = enrich_state_batch(foundation_dir=config.foundation_dir,
                                   limit=args.enrich_state,
                                   log=lambda msg: print(msg, flush=True))
        print(_json.dumps(stats, indent=1, sort_keys=True, default=str))
        return 0
    store = SocialStore(config.database)
    try:
        index = build_all(foundation_dir=config.foundation_dir, root=root,
                          store=store, limit=args.limit, force=args.force,
                          only=args.only, log=lambda msg: print(msg, flush=True))
    finally:
        store.close()
    if args.json:
        print(_json.dumps(index, indent=1, sort_keys=True, default=str))
    else:
        print(f"dossiers: built={index['built']} "
              f"skipped={index['skipped_current']} failed={index['failed']}")
        print(f"root={index['root']}")
        print(f"by_source={index['by_source']}")
        print(f"availability={index['availability']}")
    return 1 if index["failed"] and not index["built"] else 0


def _cmd_linkage(args) -> int:
    import json as _json

    from cs2archive.shorts.social.linkage import (
        linkage_summary, resolve_pending, stage_linkage,
    )
    from cs2archive.shorts.social.config import load_config
    from cs2archive.shorts.social.store import SocialStore

    config = load_config(Path(args.config))
    store = SocialStore(config.database)
    try:
        if args.from_timeline:
            from cs2archive.shorts.social.linkage import (
                evidence_from_timeline_file,
            )
            payload = evidence_from_timeline_file(Path(args.from_timeline))
            record = stage_linkage(store, payload, run_id="linkage-cli")
            print(_json.dumps(record, indent=2, sort_keys=True, default=str))
            return 0
        stats = resolve_pending(store) if args.resolve else None
        summary = linkage_summary(store)
        print(_json.dumps({"summary": summary, "resolve": stats}, indent=2,
                          sort_keys=True, default=str))
        return 0
    finally:
        store.close()


def _cmd_ceiling(args) -> int:
    import json as _json

    from cs2archive.shorts.social.ceiling import ceiling_report, format_ceiling
    from cs2archive.shorts.social.config import load_config
    from cs2archive.shorts.social.store import SocialStore

    config = load_config(Path(args.config))
    store = SocialStore(config.database)
    try:
        report = ceiling_report(store, demos_root=Path(args.demos_root))
        print(_json.dumps(report, indent=2, sort_keys=True, default=str)
              if args.json else format_ceiling(report))
        return 0
    finally:
        store.close()


def _cmd_analytics(args) -> int:
    import json as _json

    from cs2archive.shorts.social.analytics import analytics_status, ingest_analytics
    from cs2archive.shorts.social.config import load_config
    from cs2archive.shorts.social.store import SocialStore
    from cs2archive.shorts.social.safety import iso, utcnow

    config = load_config(Path(args.config))
    store = SocialStore(config.database)
    try:
        if args.status:
            print(_json.dumps(analytics_status(), indent=2, sort_keys=True))
            return 0
        own = str(config.platform("youtube").extra.get("own_channel_id") or "")
        videos = [dict(row) for row in store.conn.execute(
            "SELECT post_id, published_at FROM posts WHERE platform='youtube' "
            "AND creator_id=? AND format_label='short'", (own,))]
        run_id = store.unique_run_id(
            f"analytics-{iso(utcnow()).replace(':', '')[:15]}")
        stats = ingest_analytics(store, videos=videos, run_id=run_id,
                                 raw_root=config.raw_root)
        print(_json.dumps(stats, indent=2, sort_keys=True, default=str))
        return 0 if stats["available"] else 1
    finally:
        store.close()


def _cmd_backfill(args) -> int:
    import json as _json

    from cs2archive.shorts.social.own_backfill import backfill_own_channel
    from cs2archive.shorts.social.config import load_config
    from cs2archive.shorts.social.store import SocialStore

    config = load_config(Path(args.config))
    store = SocialStore(config.database)
    try:
        stats = backfill_own_channel(
            store, config=config, limit=args.limit,
            build_budget=args.build_budget, demos_root=Path(args.demos_root),
            timeline_dir=Path(args.timeline_dir))
        print(_json.dumps(stats, indent=2, sort_keys=True, default=str)
              if args.json else
              _json.dumps({k: v for k, v in stats.items()
                           if k not in {"results"}}, indent=2, sort_keys=True))
        return 0
    finally:
        store.close()


def _cmd_refresh(args) -> int:
    from cs2archive.shorts.social.config import load_config
    from cs2archive.shorts.social.refresh import refresh_database

    result = refresh_database(
        load_config(Path(args.config)), platforms=args.platform or None,
        min_age_hours=args.min_age_hours, max_posts=args.max_posts)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    else:
        for run in result["runs"]:
            print(f"[{run['platform']}] {run['status']} "
                  f"snapshots={run.get('snapshots_written', 0)}"
                  + (f" | {run['reason']}" if run.get("reason") else ""))
    failed = [run for run in result["runs"] if run["status"] == "failed"]
    return 1 if failed else 0


def _cmd_deepen(args) -> int:
    from cs2archive.shorts.social.config import load_config
    from cs2archive.shorts.social.deepen import stage_additional_clips

    config = load_config(Path(args.config))
    if not config.allstar_store:
        print("[deepen] no foundation.allstar_store configured")
        return 2
    stats = stage_additional_clips(config.allstar_store, config.foundation_dir,
                                   max_new=args.max_new)
    payload = stats.as_dict()
    print(json.dumps(payload, indent=2, sort_keys=True)
          if args.json else
          f"staged={stats.staged} skipped_existing={stats.skipped_existing} "
          f"rejected_no_exact={stats.rejected_no_exact} "
          f"rejected_no_round={stats.rejected_no_round} "
          f"candidates={stats.candidates} by_kills={payload['by_clip_kills']}")
    return 0


def _cmd_enrich(args) -> int:
    from cs2archive.shorts.social.collect import enrich_database

    result = enrich_database(Path(args.config))
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    else:
        print(f"links={result['links']}")
        print(f"features={result['features']}")
    return 0


def _cmd_train(args) -> int:
    from cs2archive.shorts.social.config import load_config
    from cs2archive.shorts.social.train import format_sweep, run_sweep

    report = run_sweep(load_config(Path(args.config)),
                       experimental=args.experimental, min_rows=args.min_rows)
    print(json.dumps(report, indent=2, sort_keys=True, default=str)
          if args.json else format_sweep(report))
    return 0 if report.get("status") == "fitted" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cs2archive.shorts.social_sweep",
        description="Cross-platform CS2 Shorts evidence sweep (public/authorized data only)")
    sub = parser.add_subparsers(dest="command", required=True)

    collect = sub.add_parser("collect", help="run a bounded collection sweep")
    collect.add_argument("--config", default=DEFAULT_CONFIG)
    collect.add_argument("--platform", action="append",
                         help="limit to one platform (repeatable)")
    collect.add_argument("--max-posts", type=int, default=None,
                         help="override the per-platform post cap")
    collect.add_argument("--dry-run", action="store_true",
                         help="record preflight statuses without network calls")
    collect.add_argument("--no-enrich", action="store_true",
                         help="skip feature extraction and clip-identity linking")
    collect.add_argument("--json", action="store_true")
    collect.set_defaults(func=_cmd_collect)

    report = sub.add_parser("report", help="print the coverage report")
    report.add_argument("--config", default=DEFAULT_CONFIG)
    report.add_argument("--json", action="store_true")
    report.add_argument("--out", default=None, help="also write the report to this path")
    report.set_defaults(func=_cmd_report)

    dossiers = sub.add_parser(
        "dossiers", help="build LLM-readable moment dossiers from local evidence")
    dossiers.add_argument("--config", default=DEFAULT_CONFIG)
    dossiers.add_argument("--root", default=None,
                          help="dossier output dir (default: <foundation>/dossiers)")
    dossiers.add_argument("--limit", type=int, default=0)
    dossiers.add_argument("--only", default=None, choices=["clip", "linkage"])
    dossiers.add_argument("--force", action="store_true",
                          help="rewrite dossiers that are already current")
    dossiers.add_argument("--plan-state", action="store_true",
                          help="list moments that would benefit from state enrichment")
    dossiers.add_argument("--enrich-state", type=int, default=0,
                          help="parse up to N local demos for missing state snapshots")
    dossiers.add_argument("--json", action="store_true")
    dossiers.set_defaults(func=_cmd_dossiers)

    linkage = sub.add_parser(
        "linkage", help="inspect / resume the durable short-to-demo linkage store")
    linkage.add_argument("--config", default=DEFAULT_CONFIG)
    linkage.add_argument("--resolve", action="store_true",
                         help="re-attempt pending records from the durable mirror")
    linkage.add_argument("--from-timeline", default=None,
                         help="stage evidence from a short_timeline.json path")
    linkage.set_defaults(func=_cmd_linkage)

    ceiling = sub.add_parser(
        "ceiling", help="how much of the corpus can ever be demo-linked")
    ceiling.add_argument("--config", default=DEFAULT_CONFIG)
    ceiling.add_argument("--demos-root", default="demos/hltv")
    ceiling.add_argument("--json", action="store_true")
    ceiling.set_defaults(func=_cmd_ceiling)

    analytics = sub.add_parser(
        "analytics", help="ingest authorized own-channel YouTube Analytics")
    analytics.add_argument("--config", default=DEFAULT_CONFIG)
    analytics.add_argument("--status", action="store_true")
    analytics.set_defaults(func=_cmd_analytics)

    backfill = sub.add_parser(
        "backfill", help="verify + link own-channel shorts to local demos")
    backfill.add_argument("--config", default=DEFAULT_CONFIG)
    backfill.add_argument("--limit", type=int, default=0)
    backfill.add_argument("--build-budget", type=int, default=0,
                          help="max action timelines to build from local demos")
    backfill.add_argument("--demos-root", default="demos/hltv")
    backfill.add_argument("--timeline-dir", default="data/shorts-foundation/_timelines")
    backfill.add_argument("--json", action="store_true")
    backfill.set_defaults(func=_cmd_backfill)

    refresh = sub.add_parser("refresh", help="bounded metric refresh (new snapshots)")
    refresh.add_argument("--config", default=DEFAULT_CONFIG)
    refresh.add_argument("--platform", action="append")
    refresh.add_argument("--min-age-hours", type=float, default=1.0)
    refresh.add_argument("--max-posts", type=int, default=200)
    refresh.add_argument("--json", action="store_true")
    refresh.set_defaults(func=_cmd_refresh)

    deepen = sub.add_parser(
        "deepen", help="stage extra Allstar clips from cached action timelines")
    deepen.add_argument("--config", default=DEFAULT_CONFIG)
    deepen.add_argument("--max-new", type=int, default=600)
    deepen.add_argument("--json", action="store_true")
    deepen.set_defaults(func=_cmd_deepen)

    enrich = sub.add_parser("enrich", help="re-run identity linking + feature extraction")
    enrich.add_argument("--config", default=DEFAULT_CONFIG)
    enrich.add_argument("--json", action="store_true")
    enrich.set_defaults(func=_cmd_enrich)

    train = sub.add_parser("train", help="experimental cohort-relative model sweep")
    train.add_argument("--config", default=DEFAULT_CONFIG)
    train.add_argument("--experimental", action="store_true",
                       help="required: acknowledge the sweep is experimental")
    train.add_argument("--min-rows", type=int, default=None)
    train.add_argument("--json", action="store_true")
    train.set_defaults(func=_cmd_train)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except FileNotFoundError as exc:
        print(f"[social_sweep] config error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
