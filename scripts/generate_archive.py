#!/usr/bin/env python3
"""Generate the sample historical archive shipped in the mock dataset.

Produces `backend/app/seed/archive_events.json`: a deterministic set of ~90
synthetic past announcements spread over 2024-2026, with enough repetition per
(event type, ticker) that the historical statistics reach the "limited" and
"ok" sample-size bands and the UI's sample-size flags can actually be seen.

These are **synthetic events written for testing**. They are not real quotes and
not a record of anything anyone said. Run this only to regenerate the file.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import random

OUT = pathlib.Path(__file__).resolve().parent.parent / "backend/app/seed/archive_events.json"

TEMPLATES = [
    ("tariff", "AAPL", "Announced a review of tariffs on consumer electronics imports. Apple Inc. and other manufacturers would be covered by the proposed schedule."),
    ("tariff", "NVDA", "Statement on semiconductor tariffs. Nvidia and Taiwan Semiconductor were named among affected suppliers."),
    ("tariff", "F", "Remarks on tariffs covering imported vehicles and parts, naming Ford Motor and General Motors production lines."),
    ("tariff", "GM", "Proposed tariff schedule on imported vehicles. General Motors assembly plants were referenced."),
    ("tariff", "CAT", "Comments on tariffs applied to imported construction equipment, naming Caterpillar competitors."),
    ("tariff", "DE", "Statement on agricultural equipment tariffs, referencing John Deere export exposure."),
    ("tariff", "TSM", "Remarks on chip supply chains and proposed tariffs affecting TSMC shipments."),
    ("sanction", "XOM", "Announced sanctions affecting energy trade. Exxon and Chevron operations in the region were referenced."),
    ("sanction", "CVX", "Statement on sanctions covering crude exports, naming Chevron licences."),
    ("sanction", "BA", "Sanctions announcement covering aerospace exports; Boeing deliveries were referenced."),
    ("sanction", "XLE", "Broad sanctions package touching the energy sector and oil industry export licences."),
    ("regulation", "META", "Remarks on platform regulation naming Meta Platforms and Google."),
    ("regulation", "GOOGL", "Statement on antitrust enforcement referencing Google search practices."),
    ("regulation", "JPM", "Comments on financial regulation and capital rules affecting JPMorgan."),
    ("regulation", "XLV", "Announced a review of drug pricing rules affecting pharmaceutical companies."),
    ("deregulation", "XLE", "Announced rollback of permitting rules for the energy sector and new drilling leases."),
    ("deregulation", "XLF", "Statement on easing regulation for banks and mid-size lenders."),
    ("company_mention", "AAPL", "Extended remarks on Apple Inc. manufacturing commitments in the United States."),
    ("company_mention", "TSLA", "Comments on Tesla domestic production and Elon Musk's investment plans."),
    ("company_mention", "BA", "Remarks about Boeing contract performance and delivery schedules."),
    ("company_mention", "LMT", "Statement praising Lockheed Martin programme delivery."),
    ("company_mention", "INTC", "Comments on Intel domestic fabrication investment."),
    ("defense", "LMT", "Announced a defense contract award; Lockheed Martin was named as prime contractor."),
    ("defense", "RTX", "Statement on missile defence procurement referencing Raytheon systems."),
    ("defense", "BA", "Remarks on military aircraft procurement naming Boeing."),
    ("energy", "XOM", "Statement on expanding drilling access; Exxon lease applications referenced."),
    ("energy", "CVX", "Announced approval of an LNG export terminal; Chevron was among the named participants."),
    ("energy", "XLE", "Remarks on oil production targets and the oil industry generally."),
    ("trade_deal", "CAT", "Announced a trade agreement framework covering industrial goods; Caterpillar exports referenced."),
    ("trade_deal", "DE", "Trade agreement remarks naming agricultural equipment exports and John Deere."),
    ("trade_deal", "TM", "Statement on an automotive trade agreement referencing Toyota imports."),
    ("personnel", "XLF", "Announced a nomination to a financial regulator; comments on interest rate policy."),
    ("personnel", "SPY", "Remarks on Federal Reserve leadership and interest rate expectations."),
    ("fiscal_policy", "SPY", "Statement on a proposed corporate tax cut and its effect on business investment."),
    ("fiscal_policy", "XLI", "Remarks on infrastructure spending and manufacturing investment."),
]

# How many times each (event_type, ticker) pair repeats across the archive.
# Chosen so all three sample-size bands are visible in the UI without any real
# data: >=20 is "ok", 10-19 is "limited", <10 is "unreliable".
REPEATS = {
    ("tariff", "AAPL"): 26,
    ("tariff", "NVDA"): 22,
    ("tariff", "F"): 15,
    ("tariff", "GM"): 14,
    ("tariff", "CAT"): 11,
    ("tariff", "TSM"): 12,
    ("sanction", "XOM"): 13,
    ("sanction", "CVX"): 11,
    ("regulation", "META"): 10,
    ("regulation", "GOOGL"): 12,
    ("company_mention", "AAPL"): 18,
    ("company_mention", "TSLA"): 16,
    ("company_mention", "BA"): 9,
    ("defense", "LMT"): 8,
    ("energy", "XLE"): 9,
}
DEFAULT_REPEATS = 4

VARIANTS = [
    "This is a follow-up statement restating the position in similar terms.",
    "The statement was issued alongside a fact sheet describing the proposed timetable.",
    "Officials said details would follow; no implementation date was given.",
    "The remarks were made during a press appearance and were not accompanied by an order.",
    "A written statement described the proposal as under review.",
]


def build() -> list[dict]:
    rng = random.Random(20260916)
    start = dt.date(2024, 1, 8)
    end = dt.date(2026, 8, 28)
    span = (end - start).days

    rows: list[dict] = []
    index = 0
    for event_type, ticker, text in TEMPLATES:
        repeats = REPEATS.get((event_type, ticker), DEFAULT_REPEATS)
        for cycle in range(repeats):
            index += 1
            offset = rng.randint(0, span)
            day = start + dt.timedelta(days=offset)
            hour = rng.choice([8, 10, 11, 13, 14, 15, 18, 21, 2])
            ts = dt.datetime(
                day.year, day.month, day.day, hour, rng.randint(0, 59), tzinfo=dt.timezone.utc
            )
            # Vary the wording so these are distinct events rather than exact
            # duplicates -- the content-hash dedup would otherwise collapse them.
            body = f"{text} {VARIANTS[(index + cycle) % len(VARIANTS)]} (ref {index:04d})"
            rows.append(
                {
                    "id": f"archive-{index:04d}",
                    "source": "archive",
                    "author": "Donald J. Trump" if cycle % 2 == 0 else "The White House",
                    "title": f"{event_type.replace('_', ' ').title()} statement ({ticker})",
                    "text": body,
                    "url": f"https://example.invalid/archive/{index:04d}",
                    "created_at": ts.isoformat(),
                    "event_type": event_type,
                    "primary_ticker": ticker,
                    "historical": True,
                }
            )
    rows.sort(key=lambda r: r["created_at"])
    return rows


if __name__ == "__main__":
    rows = build()
    OUT.write_text(json.dumps(rows, indent=1) + "\n")
    print(f"wrote {len(rows)} archive events to {OUT}")
