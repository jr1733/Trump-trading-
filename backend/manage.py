#!/usr/bin/env python3
"""Small operational CLI.

    python manage.py seed              # reference data + default user
    python manage.py import-archive    # load the sample historical archive
    python manage.py import FILE       # load a CSV/JSON archive of your own
    python manage.py poll              # poll every enabled source once
    python manage.py pipeline          # process everything unprocessed
    python manage.py demo              # seed + archive + poll + pipeline
    python manage.py status            # counts, source health, LLM spend
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
from app.seed import loader
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

    sub.add_parser("status").set_defaults(func=cmd_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
