"""Entity extraction and ticker matching with explicit confidence.

The rule the spec cares about: `"Apple Inc." -> AAPL HIGH`, but `"apple"` used
generically -> `AAPL LOW`. We get that by marking aliases that collide with
ordinary English (`apple`, `delta`, `target`, `gap`) as *ambiguous* and only
promoting them above LOW when the surrounding text corroborates the company
reading -- a corporate suffix, a cashtag, or a nearby business word.

LOW matches are stored and displayed but excluded from signals by default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import EntityAlias, Event, EventTicker

CONFIDENCE_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}

CORPORATE_SUFFIXES = (
    " inc", " inc.", " corp", " corp.", " corporation", " co.", " company",
    " ltd", " plc", " llc", " group", " holdings", " motors", " technologies",
)

# Words that, near an ambiguous alias, make the company reading likely.
BUSINESS_CONTEXT = (
    "shares", "stock", "shareholder", "ceo", "earnings", "revenue", "market cap",
    "nasdaq", "nyse", "company", "quarterly", "investors", "factory", "plant",
    "tariff", "merger", "acquisition", "board", "layoffs", "iphone", "products",
)

_CASHTAG = re.compile(r"\$([A-Z]{1,5})\b")
_TOKEN_SPLIT = re.compile(r"[^a-z0-9']+")


@dataclass
class TickerMatch:
    ticker: str
    confidence: str
    matched_alias: str
    source: str = "rules"

    def rank(self) -> int:
        return CONFIDENCE_ORDER[self.confidence]


def _alias_present(blob: str, alias: str) -> bool:
    """Whole-token containment, so 'gm' does not match inside 'programme'."""
    return re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", blob) is not None


def _corroborated(blob: str, alias: str) -> bool:
    """Does the text support reading an ambiguous alias as a company?"""
    for suffix in CORPORATE_SUFFIXES:
        if f"{alias}{suffix}" in blob:
            return True
    match = re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", blob)
    if match is None:
        return False
    window = blob[max(0, match.start() - 120) : match.end() + 120]
    return any(word in window for word in BUSINESS_CONTEXT)


def match_tickers(
    text: str,
    title: str | None,
    aliases: list[EntityAlias],
    *,
    claude_tickers: list[str] | None = None,
) -> list[TickerMatch]:
    """Match a text against the alias table. Highest confidence per ticker wins."""
    blob = f"{title or ''} {text or ''}".lower()
    best: dict[str, TickerMatch] = {}

    def offer(candidate: TickerMatch) -> None:
        current = best.get(candidate.ticker)
        if current is None or candidate.rank() > current.rank():
            best[candidate.ticker] = candidate

    # 1. Explicit cashtags are unambiguous by construction.
    for symbol in _CASHTAG.findall(f"{title or ''} {text or ''}"):
        offer(TickerMatch(symbol.upper(), "HIGH", f"${symbol.upper()}"))

    # 2. Alias table.
    for alias in aliases:
        needle = alias.alias.lower()
        if not _alias_present(blob, needle):
            continue
        if alias.user_corrected:
            # A correction the user made is authoritative and never downgraded.
            offer(TickerMatch(alias.ticker.upper(), alias.confidence, alias.alias, "user"))
            continue
        if alias.ambiguous:
            confidence = "MEDIUM" if _corroborated(blob, needle) else "LOW"
            # An ambiguous alias never outranks its configured ceiling.
            if CONFIDENCE_ORDER[confidence] > CONFIDENCE_ORDER[alias.confidence]:
                confidence = alias.confidence
        else:
            confidence = alias.confidence
        offer(TickerMatch(alias.ticker.upper(), confidence, alias.alias))

    # 3. Tickers the model named. Corroborated by the alias table -> HIGH,
    #    otherwise MEDIUM: the model saw the same text we did, but we did not
    #    independently verify the symbol.
    for symbol in claude_tickers or []:
        upper = symbol.upper()
        confidence = "HIGH" if upper in best else "MEDIUM"
        offer(TickerMatch(upper, confidence, symbol, "claude"))

    return sorted(best.values(), key=lambda m: (-m.rank(), m.ticker))


def load_aliases(db: Session) -> list[EntityAlias]:
    return list(db.execute(select(EntityAlias)).scalars())


def apply_matches(db: Session, event: Event, matches: list[TickerMatch]) -> list[EventTicker]:
    """Persist matches idempotently, preserving any user correction already stored."""
    existing = {row.ticker: row for row in event.tickers}
    written: list[EventTicker] = []
    for match in matches:
        row = existing.get(match.ticker)
        if row is None:
            row = EventTicker(
                event_id=event.id,
                ticker=match.ticker,
                confidence=match.confidence,
                matched_alias=match.matched_alias,
                source=match.source,
            )
            db.add(row)
        elif not row.user_corrected:
            row.confidence = match.confidence
            row.matched_alias = match.matched_alias
            row.source = match.source
        written.append(row)
    db.flush()
    return written


def extract_entities(text: str, title: str | None, aliases: list[EntityAlias]) -> list[str]:
    """Entity names present in the text, from the alias table. Order is stable."""
    blob = f"{title or ''} {text or ''}".lower()
    found: list[str] = []
    for alias in aliases:
        needle = alias.alias.lower()
        if _alias_present(blob, needle) and alias.alias not in found:
            if alias.ambiguous and not _corroborated(blob, needle):
                continue
            found.append(alias.alias)
    return found


def tokenize(text: str) -> set[str]:
    """Token set used by the Phase 1 lexical novelty/similarity measure."""
    return {t for t in _TOKEN_SPLIT.split((text or "").lower()) if len(t) > 3}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
