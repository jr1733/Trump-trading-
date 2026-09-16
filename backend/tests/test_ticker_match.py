"""Ticker matching and confidence, including the Apple Inc. / apple pie case."""

from __future__ import annotations

from app.models import EntityAlias
from app.pipeline.ticker_match import (
    extract_entities,
    jaccard,
    match_tickers,
    tokenize,
)


def aliases() -> list[EntityAlias]:
    return [
        EntityAlias(alias="Apple Inc.", ticker="AAPL", confidence="HIGH", ambiguous=False),
        EntityAlias(alias="apple", ticker="AAPL", confidence="MEDIUM", ambiguous=True),
        EntityAlias(alias="Nvidia", ticker="NVDA", confidence="HIGH", ambiguous=False),
        EntityAlias(alias="ford", ticker="F", confidence="MEDIUM", ambiguous=True),
        EntityAlias(alias="General Motors", ticker="GM", confidence="HIGH", ambiguous=False),
    ]


def only(matches, ticker):
    return next((m for m in matches if m.ticker == ticker), None)


def test_full_company_name_is_high_confidence():
    matches = match_tickers("Apple Inc. will build a plant here.", None, aliases())
    assert only(matches, "AAPL").confidence == "HIGH"


def test_generic_word_use_is_low_confidence():
    matches = match_tickers("They served an apple pie that was wonderful.", None, aliases())
    match = only(matches, "AAPL")
    assert match is not None, "the LOW match is still stored and displayed"
    assert match.confidence == "LOW"


def test_ambiguous_alias_promoted_by_business_context():
    matches = match_tickers(
        "apple shares moved after the announcement about earnings.", None, aliases()
    )
    assert only(matches, "AAPL").confidence == "MEDIUM"


def test_ambiguous_alias_never_exceeds_its_ceiling():
    # "apple" is capped at MEDIUM even with heavy corroboration.
    matches = match_tickers(
        "apple stock, apple earnings, apple revenue, apple shareholders", None, aliases()
    )
    assert only(matches, "AAPL").confidence == "MEDIUM"


def test_cashtag_is_high_confidence_without_an_alias_row():
    matches = match_tickers("Watching $MSFT closely today.", None, aliases())
    assert only(matches, "MSFT").confidence == "HIGH"


def test_alias_matching_respects_token_boundaries():
    # "ford" must not match inside "Stafford" or "afford".
    matches = match_tickers("We cannot afford this in Stafford County.", None, aliases())
    assert only(matches, "F") is None


def test_highest_confidence_wins_per_ticker():
    matches = match_tickers("apple pie, but also Apple Inc. investment", None, aliases())
    assert only(matches, "AAPL").confidence == "HIGH"
    assert len([m for m in matches if m.ticker == "AAPL"]) == 1


def test_claude_ticker_corroborated_by_rules_is_high():
    matches = match_tickers(
        "Nvidia chips are under review.", None, aliases(), claude_tickers=["NVDA"]
    )
    assert only(matches, "NVDA").confidence == "HIGH"


def test_claude_only_ticker_is_medium():
    matches = match_tickers(
        "The chip supply chain faces new rules.", None, aliases(), claude_tickers=["AMD"]
    )
    match = only(matches, "AMD")
    assert match.confidence == "MEDIUM"
    assert match.source == "claude"


def test_user_corrected_alias_is_authoritative():
    rows = aliases()
    rows.append(
        EntityAlias(
            alias="apple", ticker="AAPL", confidence="HIGH", ambiguous=True, user_corrected=True
        )
    )
    matches = match_tickers("an apple a day", None, rows)
    assert only(matches, "AAPL").confidence == "HIGH"
    assert only(matches, "AAPL").source == "user"


def test_extract_entities_skips_uncorroborated_ambiguous_aliases():
    found = extract_entities("an apple a day keeps things simple", None, aliases())
    assert "apple" not in found

    found = extract_entities("Apple Inc. and Nvidia both commented", None, aliases())
    assert "Apple Inc." in found and "Nvidia" in found


def test_jaccard_similarity_bounds():
    a = tokenize("tariffs on imported semiconductors are under review")
    assert jaccard(a, a) == 1.0
    assert jaccard(a, tokenize("")) == 0.0
    assert 0.0 < jaccard(a, tokenize("tariffs on imported vehicles are under review")) < 1.0
