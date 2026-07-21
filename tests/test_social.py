"""Unit tests for the pure social-scoring helpers (no network).

Only the lexicon/scoring functions are covered here; the scrapers need live
sources and are exercised by the app itself.
"""

import math

import pytest

from python_api.social import (
    classify_sentiment,
    confidence_tier_from_score,
    extract_tickers,
    weighted_source_signal,
)

# ---------------------------------------------------------------------------
# classify_sentiment
# ---------------------------------------------------------------------------


def test_classify_sentiment_bullish():
    assert classify_sentiment("Buying calls, this thing is going to the moon") == "bullish"
    assert classify_sentiment("bullish breakout, going long") == "bullish"


def test_classify_sentiment_bearish():
    assert classify_sentiment("dump this, puts printing, heading down") == "bearish"
    assert classify_sentiment("bearish, time to sell") == "bearish"


def test_classify_sentiment_neutral():
    # No lexicon words at all
    assert classify_sentiment("AAPL earnings transcript is available") == "neutral"
    # Balanced bull/bear counts tie out to neutral
    assert classify_sentiment("bull bear") == "neutral"


def test_classify_sentiment_empty_and_none():
    assert classify_sentiment("") == "neutral"
    assert classify_sentiment(None) == "neutral"


# ---------------------------------------------------------------------------
# extract_tickers
# ---------------------------------------------------------------------------


def test_extract_tickers_cashtags_and_uppercase():
    out = extract_tickers("$NVDA and AAPL are ripping today")
    assert out == {"NVDA", "AAPL"}


def test_extract_tickers_lowercase_needs_cashtag():
    # A lowercase token is only accepted with a $ prefix.
    assert extract_tickers("tsla to the moon") == set()
    assert extract_tickers("$tsla to the moon") == {"TSLA"}


def test_extract_tickers_exclusion_list():
    # CEO / IT / NEWS are in TICKER_EXCLUDE despite matching the pattern.
    assert extract_tickers("CEO said IT was NEWS") == set()


# ---------------------------------------------------------------------------
# weighted_source_signal / confidence_tier_from_score
# ---------------------------------------------------------------------------


def test_weighted_source_signal_empty_is_zero():
    assert weighted_source_signal({}) == 0.0
    assert weighted_source_signal({"reddit": 0}) == 0.0


def test_weighted_source_signal_known_value():
    # reddit weight is 1.0, so a single source is just log1p(count).
    assert weighted_source_signal({"reddit": 4}) == pytest.approx(math.log1p(4))


def test_weighted_source_signal_more_sources_score_higher():
    one = weighted_source_signal({"reddit": 5})
    two = weighted_source_signal({"reddit": 5, "stocktwits": 5})
    assert two > one


def test_confidence_tiers():
    assert confidence_tier_from_score(3.0) == "A"
    assert confidence_tier_from_score(2.6) == "A"
    assert confidence_tier_from_score(2.0) == "B"
    assert confidence_tier_from_score(1.8) == "B"
    assert confidence_tier_from_score(1.0) == "C"
    assert confidence_tier_from_score(0.0) == "C"
