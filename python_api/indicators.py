"""Technical indicators and trigger flags.

Pure pandas/numpy functions extracted from main.py: no module-level state,
config, or I/O, so they're trivially unit-testable. Given an OHLCV frame they
compute SMA/RSI/MACD/RVOL and derive the technical trigger flags and labels the
screener, alerts, and /technical endpoints surface.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    close = out["close"]
    volume = out["volume"]

    out["sma20"] = close.rolling(20).mean()
    out["sma50"] = close.rolling(50).mean()
    out["sma200"] = close.rolling(200).mean()

    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out["rsi14"] = 100 - (100 / (1 + rs))

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    out["macd"] = ema12 - ema26
    out["macd_signal"] = out["macd"].ewm(span=9, adjust=False).mean()
    out["macd_hist"] = out["macd"] - out["macd_signal"]

    out["avg_volume20"] = volume.rolling(20).mean()
    out["rvol"] = volume / out["avg_volume20"].shift(1)
    out["daily_change_pct"] = close.pct_change() * 100
    return out


def compute_technical_trigger_flags(ind: pd.DataFrame) -> dict[str, bool]:
    latest = ind.iloc[-1]
    prev = ind.iloc[-2] if len(ind) > 1 else latest

    rolling_high_20 = ind["high"].rolling(20).max().shift(1)
    rolling_low_20 = ind["low"].rolling(20).min().shift(1)
    last_20_high = rolling_high_20.iloc[-1] if len(rolling_high_20) else np.nan
    last_20_low = rolling_low_20.iloc[-1] if len(rolling_low_20) else np.nan

    return {
        "rsi_overbought": bool(pd.notna(latest["rsi14"]) and latest["rsi14"] > 70),
        "rsi_oversold": bool(pd.notna(latest["rsi14"]) and latest["rsi14"] < 30),
        "breakout_20d": bool(pd.notna(last_20_high) and latest["close"] > last_20_high),
        "breakdown_20d": bool(pd.notna(last_20_low) and latest["close"] < last_20_low),
        "bullish_crossover_50": bool(
            pd.notna(prev["sma50"])
            and pd.notna(prev["close"])
            and pd.notna(latest["sma50"])
            and pd.notna(latest["close"])
            and prev["close"] <= prev["sma50"]
            and latest["close"] > latest["sma50"]
        ),
        "bearish_crossover_50": bool(
            pd.notna(prev["sma50"])
            and pd.notna(prev["close"])
            and pd.notna(latest["sma50"])
            and pd.notna(latest["close"])
            and prev["close"] >= prev["sma50"]
            and latest["close"] < latest["sma50"]
        ),
        "macd_bullish_cross": bool(
            pd.notna(prev["macd"])
            and pd.notna(prev["macd_signal"])
            and pd.notna(latest["macd"])
            and pd.notna(latest["macd_signal"])
            and prev["macd"] <= prev["macd_signal"]
            and latest["macd"] > latest["macd_signal"]
        ),
        "macd_bearish_cross": bool(
            pd.notna(prev["macd"])
            and pd.notna(prev["macd_signal"])
            and pd.notna(latest["macd"])
            and pd.notna(latest["macd_signal"])
            and prev["macd"] >= prev["macd_signal"]
            and latest["macd"] < latest["macd_signal"]
        ),
        "unusual_volume": bool(pd.notna(latest["rvol"]) and latest["rvol"] >= 2),
    }


def trigger_labels(flags: dict[str, bool]) -> list[str]:
    labels = [
        ("RSI > 70", "rsi_overbought"),
        ("RSI < 30", "rsi_oversold"),
        ("20D Breakout", "breakout_20d"),
        ("20D Breakdown", "breakdown_20d"),
        ("Crossed Above 50 SMA", "bullish_crossover_50"),
        ("Crossed Below 50 SMA", "bearish_crossover_50"),
        ("MACD Bullish Cross", "macd_bullish_cross"),
        ("MACD Bearish Cross", "macd_bearish_cross"),
        ("Unusual Volume (RVOL >= 2)", "unusual_volume"),
    ]
    return [label for label, key in labels if flags.get(key, False)]
