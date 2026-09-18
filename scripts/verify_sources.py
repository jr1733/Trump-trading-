#!/usr/bin/env python3
"""Local wrapper around `app.ops.verify`.

The real implementation lives in `backend/app/ops/verify.py` so that it is
present *inside the api container* -- the image is built from `backend/`, so a
repo-root script is not in it, and the deploy runbook needs to run this on the
server. This file keeps `make verify-sources` working from a checkout with a
virtualenv.

    make verify-sources ARGS="--all-sources -v"

On the server, use the container copy instead:

    docker compose run --rm api python manage.py verify-sources --all-sources -v
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.ops import verify  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=verify.__doc__.split("\n")[0])
    parser.add_argument("--sources", help="comma-separated keys (default: ENABLED_SOURCES)")
    parser.add_argument("--all-sources", action="store_true", help="probe every known adapter")
    parser.add_argument("--market-only", action="store_true")
    parser.add_argument("--sources-only", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true", help="show tracebacks and samples")
    args = parser.parse_args()

    return verify.run(
        sources=args.sources,
        all_sources=args.all_sources,
        market_only=args.market_only,
        sources_only=args.sources_only,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    raise SystemExit(main())
