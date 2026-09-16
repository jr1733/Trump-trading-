"""Rule-based relevance filter and rule-based sentiment.

Two jobs, both deliberately free of any LLM:

1. `score_relevance` is the cheap gate in front of the model. Nothing reaches
   Claude without passing it, which is the single biggest cost lever we have.
2. `rule_based_sentiment` exists for **backtesting**. Claude's training data may
   contain knowledge of what happened after a 2024 event, so a backtest scored by
   the model is potentially contaminated. The rule-based scorer has no such
   knowledge, which makes it the honest default for historical evaluation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Terms that make an item potentially market-relevant. Weight reflects how
# reliably the term implies an economic consequence, not how dramatic it sounds.
TOPIC_TERMS: dict[str, float] = {
    "tariff": 1.0, "tariffs": 1.0, "trade deal": 0.9, "trade war": 0.9,
    "sanction": 0.9, "sanctions": 0.9, "embargo": 0.8, "export control": 0.8,
    "quota": 0.6, "subsidy": 0.7, "subsidies": 0.7, "bailout": 0.8,
    "regulation": 0.7, "deregulate": 0.7, "deregulation": 0.7, "executive order": 0.8,
    "antitrust": 0.8, "merger": 0.7, "investigation": 0.5, "lawsuit": 0.5,
    "tax": 0.7, "taxes": 0.7, "corporate tax": 0.9, "tax cut": 0.9,
    "federal reserve": 0.8, "interest rate": 0.8, "inflation": 0.6,
    "oil": 0.6, "opec": 0.7, "drilling": 0.6, "pipeline": 0.6, "lng": 0.6,
    "defense contract": 0.9, "defence contract": 0.9, "military spending": 0.7,
    "semiconductor": 0.8, "chips": 0.6, "chip act": 0.8,
    "immigration": 0.4, "border": 0.3,
    "contract": 0.4, "procurement": 0.6, "billion": 0.4, "investment": 0.4,
    "nationalize": 0.9, "price cap": 0.8, "ban": 0.6, "approve": 0.4,
}

# Items that are purely these get filtered out even if a topic term appears.
NEGATIVE_TERMS = (
    "golf", "birthday", "condolence", "medal of freedom", "thanksgiving turkey",
    "rally schedule", "poll numbers", "ratings",
)

POSITIVE_SENTIMENT = {
    "great": 0.4, "tremendous": 0.5, "strong": 0.35, "booming": 0.6, "record": 0.4,
    "approve": 0.4, "approved": 0.4, "deal": 0.3, "agreement": 0.35, "invest": 0.4,
    "expand": 0.35, "cut taxes": 0.7, "tax cut": 0.7, "deregulate": 0.6,
    "win": 0.4, "success": 0.4, "boost": 0.45, "support": 0.3, "lower rates": 0.5,
}
NEGATIVE_SENTIMENT = {
    "tariff": -0.5, "tariffs": -0.5, "sanction": -0.6, "sanctions": -0.6,
    "ban": -0.5, "banned": -0.5, "investigate": -0.45, "investigation": -0.45,
    "lawsuit": -0.4, "sue": -0.4, "terrible": -0.6, "disaster": -0.6,
    "failing": -0.5, "fraud": -0.6, "penalty": -0.45, "fine": -0.35,
    "restrict": -0.45, "restriction": -0.45, "block": -0.45, "halt": -0.5,
    "crackdown": -0.55, "rip off": -0.5, "ripping off": -0.5, "unfair": -0.4,
}

NEGATORS = ("not", "no", "never", "won't", "will not", "wouldn't", "cannot", "can't", "ending")

_WORD = re.compile(r"[a-z][a-z'\-]+")


@dataclass
class RelevanceVerdict:
    relevant: bool
    score: float
    reason: str
    matched_terms: list[str]


def score_relevance(text: str, title: str | None = None, *, threshold: float = 0.3) -> RelevanceVerdict:
    """Cheap keyword/entity gate. Returns a 0-1 score and a human-readable reason."""
    blob = f"{title or ''} {text or ''}".lower()
    if not blob.strip():
        return RelevanceVerdict(False, 0.0, "empty text", [])

    matched = [term for term in TOPIC_TERMS if term in blob]
    if not matched:
        return RelevanceVerdict(False, 0.0, "no market-relevant terms found", [])

    # Score on the strongest two matches -- a long article mentioning "tax" ten
    # times is not ten times more relevant.
    weights = sorted((TOPIC_TERMS[t] for t in matched), reverse=True)
    score = min(1.0, weights[0] + 0.25 * sum(weights[1:3]))

    negatives = [term for term in NEGATIVE_TERMS if term in blob]
    if negatives and score < 0.8:
        score *= 0.4
        return RelevanceVerdict(
            score >= threshold,
            round(score, 3),
            f"matched {', '.join(matched[:3])} but also {negatives[0]}",
            matched,
        )

    return RelevanceVerdict(
        score >= threshold,
        round(score, 3),
        f"matched {', '.join(matched[:4])}",
        matched,
    )


def rule_based_sentiment(text: str, title: str | None = None) -> float:
    """Lexicon sentiment in [-1, 1]. No LLM, therefore no look-ahead contamination.

    Simple negation handling: a sentiment term within three words after a
    negator has its sign flipped ("no new tariffs" is not bearish).
    """
    blob = f"{title or ''} {text or ''}".lower()
    if not blob.strip():
        return 0.0
    words = _WORD.findall(blob)

    total = 0.0
    hits = 0
    for phrase, weight in list(POSITIVE_SENTIMENT.items()) + list(NEGATIVE_SENTIMENT.items()):
        if phrase not in blob:
            continue
        head = phrase.split()[0]
        try:
            index = words.index(head)
        except ValueError:
            index = -1
        negated = False
        if index > 0:
            window = words[max(0, index - 3) : index]
            negated = any(n in window for n in NEGATORS)
        total += -weight if negated else weight
        hits += 1

    if hits == 0:
        return 0.0
    # Average, then squash so a pile-up of weak terms cannot reach +-1.
    average = total / hits
    return round(max(-1.0, min(1.0, average * 1.2)), 3)
