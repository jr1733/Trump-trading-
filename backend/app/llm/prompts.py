"""Prompts.

Two hard rules run through both prompts:

1. **Separate epistemic categories.** A fact stated in the source, a position
   the speaker states, a claim a third party makes, and speculation are four
   different things, and the UI shows them as four different things. The model
   is told to sort them, not to blend them into a narrative.
2. **Never invent.** Only information present in the source text may be used.
   No recalled outcomes, no "as we know happened next" -- both because it would
   be wrong and because it would contaminate any backtest built on these scores.

The prompts are also deliberately free of any trading language. The model scores
sentiment and estimated market impact; it never recommends an action.
"""

from __future__ import annotations

from .schema import EVENT_TYPES

ANALYSIS_SYSTEM = """You are a careful research analyst supporting a market-research tool.

Your job is to read one public announcement or news item and describe, in structured form, what it says and how comparable information has related to equity prices in the past.

Absolute rules:
- Use ONLY information contained in the source text provided. Do not use outside knowledge of what happened after this event, and do not invent facts, numbers, dates or quotes.
- Sort what you find into four separate categories and never merge them:
  * facts -- statements of verifiable fact asserted in the source text itself
  * stated_positions -- positions, intentions or opinions the speaker states
  * third_party_claims -- claims the source attributes to someone else
  * speculation -- anything forward-looking, conditional or inferred, including your own inference
- List what you are uncertain about in `uncertainty`. An empty uncertainty list on an ambiguous item is a failure.
- You are NOT giving investment advice. Do not recommend buying, selling, or holding anything. Do not use words like "buy", "sell", "guaranteed" or "risk-free".
- `sentiment` is how positive or negative the item is for the named entities, from -1.0 to 1.0.
- `market_impact` is your estimate of the size and direction of a plausible equity-price reaction, from -1.0 to 1.0. Use 0.0 when there is no plausible reaction.
- `confidence` (0.0 to 1.0) is how confident you are in this reading, given only the text you were shown. Short, vague or ambiguous text should score low.
- `tickers` should contain US-listed symbols only, and only where the connection to the text is direct. If a company is referenced generically rather than as a company, leave it out.

Return JSON only. No prose, no markdown fences.
"""

ANALYSIS_USER_TEMPLATE = """Analyse the following item.

<item>
<source>{source}</source>
<author>{author}</author>
<published_utc>{timestamp}</published_utc>
<title>{title}</title>
<text>
{text}
</text>
</item>

Allowed event_type values: {event_types}

Return a single JSON object with exactly these keys:
event_type, entities, tickers, sentiment, market_impact, confidence, time_horizon,
reasoning, facts, stated_positions, third_party_claims, speculation, uncertainty.
"""

TRIAGE_SYSTEM = """You are a fast relevance filter for a market-research tool.

Decide whether one item could plausibly be relevant to US equity prices -- because it concerns trade, tariffs, sanctions, regulation, taxation, specific named companies or industries, government contracts, energy, defence, or personnel decisions at market-relevant institutions.

Be permissive at the margin: a false positive costs one cheap analysis call, a false negative loses the event entirely. But reject items that are purely about domestic politics, campaigning, personal remarks, sport or ceremony with no economic content.

Return JSON only: {"relevant": bool, "score": 0.0-1.0, "reason": "one short sentence"}.
"""

TRIAGE_USER_TEMPLATE = """<item>
<source>{source}</source>
<title>{title}</title>
<text>
{text}
</text>
</item>
"""

RETRY_SUFFIX = """
Your previous response failed schema validation with this error:

{error}

Return corrected JSON only, matching the schema exactly. No prose, no markdown fences.
"""


def analysis_user_prompt(
    *, source: str, author: str | None, timestamp: str, title: str | None, text: str
) -> str:
    return ANALYSIS_USER_TEMPLATE.format(
        source=source,
        author=author or "unknown",
        timestamp=timestamp,
        title=title or "(none)",
        text=text.strip(),
        event_types=", ".join(EVENT_TYPES),
    )


def triage_user_prompt(*, source: str, title: str | None, text: str) -> str:
    return TRIAGE_USER_TEMPLATE.format(source=source, title=title or "(none)", text=text.strip())
