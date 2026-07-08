"""Unit tests for the pure technical-indicator module (no network, no stack)."""

import pandas as pd

from python_api.indicators import (
    compute_indicators,
    compute_technical_trigger_flags,
    trigger_labels,
)


def _ohlcv(closes, volume=1_000_000):
    vol = [volume] * len(closes)
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "volume": vol,
        }
    )


def _trend(start, step, n=80):
    """Mostly-`step` moves with a pullback every 4th bar, so there are real
    gains AND losses in the window (RSI is only defined when both exist)."""
    price, out = float(start), []
    for i in range(n):
        price += step if i % 4 != 0 else -step / 3
        out.append(round(price, 2))
    return out


def test_compute_indicators_adds_expected_columns():
    out = compute_indicators(_ohlcv(_trend(100, 2, n=250)))
    for col in ["sma20", "sma50", "sma200", "rsi14", "macd", "macd_signal", "rvol", "daily_change_pct"]:
        assert col in out.columns
    last = out.iloc[-1]
    assert 0 <= last["rsi14"] <= 100
    assert last["close"] > last["sma20"]  # steady uptrend sits above its short SMA


def test_rsi_flags_overbought_on_strong_uptrend():
    up = compute_indicators(_ohlcv(_trend(100, 3))).iloc[-1]
    assert up["rsi14"] > 70


def test_rsi_flags_oversold_on_strong_downtrend():
    down = compute_indicators(_ohlcv(_trend(300, -3))).iloc[-1]
    assert down["rsi14"] < 30


def test_unusual_volume_trigger_fires_on_volume_spike():
    df = _ohlcv([100 + (i % 3) for i in range(60)])
    df.loc[df.index[-1], "volume"] = 5_000_000  # 5x the baseline
    flags = compute_technical_trigger_flags(compute_indicators(df))
    assert flags["unusual_volume"] is True


def test_trigger_labels_map_flags_to_human_strings():
    flags = {"rsi_overbought": True, "unusual_volume": True, "macd_bullish_cross": False}
    labels = trigger_labels(flags)
    assert "RSI > 70" in labels
    assert "Unusual Volume (RVOL >= 2)" in labels
    assert "MACD Bullish Cross" not in labels
