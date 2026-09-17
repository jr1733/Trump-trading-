#!/usr/bin/env python3
"""Small operational CLI.

    python manage.py seed              # reference data + default user
    python manage.py import-archive    # load the sample historical archive
    python manage.py import FILE       # load a CSV/JSON archive of your own
    python manage.py poll              # poll every enabled source once
    python manage.py pipeline          # process everything unprocessed
    python manage.py demo              # seed + archive + poll + pipeline + embed
    python manage.py embed             # (re)build embeddings for similarity search
    python manage.py status            # counts, source health, LLM spend
    python manage.py purge-mock        # delete synthetic events, signals, prices

`seed` loads REFERENCE DATA ONLY -- tickers, aliases, entities, the user,
watchlist, alert rules, preferences and source rows. It loads no events and no
prices, so it is safe to run on production; that is why docker-compose runs it
on every start. `import-archive` and `demo` are the ones that load synthetic
events -- never run those against a production database.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

from sqlalchemy import func, select

from app.db import session_scope
from app.llm.fake import build_client
from app.models import Event, LLMUsage, Notification, RawEvent, Signal, SourceHealth
from app.pipeline import runner
from app.seed import loader, purge
from app.sources import registry


def cmd_seed(_: argparse.Namespace) -> None:
    with session_scope() as db:
        counts = loader.seed_reference_data(db)
        user = loader.seed_user(db)
    print(f"seeded reference data: {counts}; user={user.id}")


def cmd_import_archive(_: argparse.Namespace) -> None:
    with session_scope() as db:
        inserted = loader.seed_sample_archive(db)
    print(f"imported {inserted} new archive rows")


def cmd_import(args: argparse.Namespace) -> None:
    path = pathlib.Path(args.path)
    if not path.exists():
        sys.exit(f"no such file: {path}")
    rows = loader.load_archive_file(path)
    with session_scope() as db:
        inserted = loader.import_archive(db, rows, source_key=args.source)
    print(f"read {len(rows)} rows, inserted {inserted} new")


def cmd_poll(_: argparse.Namespace) -> None:
    with session_scope() as db:
        results = registry.poll_all(db)
    for row in results:
        print(f"  {row['source']:<18} status={row['status']:<12} new={row['new_items']}")


def cmd_pipeline(args: argparse.Namespace) -> None:
    with session_scope() as db:
        report = runner.run_pipeline(db, limit=args.limit, client=build_client())
    for key, value in report.as_dict().items():
        print(f"  {key}: {value}")


def cmd_demo(args: argparse.Namespace) -> None:
    cmd_seed(args)
    cmd_import_archive(args)
    cmd_poll(args)
    print("processing archive + live items (this builds the historical sample)...")
    cmd_pipeline(args)
    print("embedding events for similarity search...")
    cmd_embed(args)


def cmd_embed(args: argparse.Namespace) -> None:
    from app.embeddings.service import EmbeddingService

    with session_scope() as db:
        service = EmbeddingService(db)
        written = service.backfill(limit=args.limit)
        remaining = len(service.pending_events(limit=100000))
        provider, dim, label = service.provider.name, service.provider.dim, service.provider.label
    print(f"  provider: {provider} ({dim}d)")
    print(f"  measure:  {label}")
    print(f"  embedded: {written}  remaining: {remaining}")


def cmd_status(_: argparse.Namespace) -> None:
    with session_scope() as db:
        counts = {
            "raw_events": db.execute(select(func.count(RawEvent.id))).scalar(),
            "unprocessed_raw": db.execute(
                select(func.count(RawEvent.id)).where(RawEvent.processed.is_(False))
            ).scalar(),
            "events": db.execute(select(func.count(Event.id))).scalar(),
            "historical_events": db.execute(
                select(func.count(Event.id)).where(Event.is_historical.is_(True))
            ).scalar(),
            "signals": db.execute(select(func.count(Signal.id))).scalar(),
            "notifications": db.execute(select(func.count(Notification.id))).scalar(),
        }
        spend = db.execute(
            select(
                func.count(LLMUsage.id),
                func.coalesce(func.sum(LLMUsage.estimated_cost_usd), 0.0),
            )
        ).one()
        health = list(db.execute(select(SourceHealth)).scalars())

    for key, value in counts.items():
        print(f"  {key}: {value}")
    print(f"  llm_calls: {spend[0]}  estimated_cost_usd: {float(spend[1]):.4f}")
    print("  sources:")
    for row in health:
        print(f"    {row.source_key:<18} {row.status:<12} failures={row.consecutive_failures}")


def cmd_purge_mock(args: argparse.Namespace) -> None:
    """Delete synthetic events, analyses, signals, matches, notifications and
    prices. Reference data and configuration are left alone."""
    with session_scope() as db:
        counts = purge.count_synthetic(db)

        total = sum(counts.values())
        label = "would delete" if args.dry_run else "deleting"
        print(f"{label} (sources: {', '.join(purge.SYNTHETIC_SOURCES)}; "
              f"price providers: {', '.join(purge.SYNTHETIC_PRICE_PROVIDERS)}):")
        for key, value in counts.items():
            print(f"  {key:<20} {value}")
        print(f"  {'TOTAL':<20} {total}")

        if args.dry_run:
            print("\ndry run: nothing was deleted. Re-run without --dry-run to apply.")
            return
        if total == 0:
            print("\nnothing to delete.")
            return

        purge.purge_synthetic(db, all_notifications=args.all_notifications)
        print("\ndone. Reference data (tickers, aliases, entities, user, watchlist,")
        print("alert rules, preferences, sources) was NOT touched.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("seed").set_defaults(func=cmd_seed)
    sub.add_parser("import-archive").set_defaults(func=cmd_import_archive)

    imp = sub.add_parser("import")
    imp.add_argument("path")
    imp.add_argument("--source", default="archive")
    imp.set_defaults(func=cmd_import)

    sub.add_parser("poll").set_defaults(func=cmd_poll)

    pipe = sub.add_parser("pipeline")
    pipe.add_argument("--limit", type=int, default=500)
    pipe.set_defaults(func=cmd_pipeline)

    demo = sub.add_parser("demo")
    demo.add_argument("--limit", type=int, default=2000)
    demo.set_defaults(func=cmd_demo)

    embed = sub.add_parser("embed")
    embed.add_argument("--limit", type=int, default=5000)
    embed.set_defaults(func=cmd_embed)

    sub.add_parser("status").set_defaults(func=cmd_status)

    purge_cmd = sub.add_parser(
        "purge-mock", help="delete synthetic events, signals and prices"
    )
    purge_cmd.add_argument(
        "--dry-run", action="store_true", help="print counts and delete nothing"
    )
    purge_cmd.add_argument(
        "--all-notifications",
        action="store_true",
        help="also clear digests and system alerts, which have no event_id and "
             "therefore survive the event-scoped delete",
    )
    purge_cmd.set_defaults(func=cmd_purge_mock)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
