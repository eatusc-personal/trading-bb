import os

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from openbb import obb

STREAMLIT_PROVIDER = os.getenv("STREAMLIT_PROVIDER", "").strip()

st.set_page_config(page_title="OpenBB Scanner Dashboard", layout="wide")
st.title("OpenBB Scanner Dashboard")
st.caption("Local Streamlit app powered by OpenBB.")


def compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


@st.cache_data(ttl=120)
def fetch_history(symbol: str, provider: str | None = None) -> pd.DataFrame:
    resolved_provider = (provider or STREAMLIT_PROVIDER).strip()
    data = obb.equity.price.historical(symbol=symbol, provider=resolved_provider) if resolved_provider else obb.equity.price.historical(symbol=symbol)
    df = data.to_df().copy()
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").set_index("date")
    else:
        df = df.sort_index()
    return df


@st.cache_data(ttl=120)
def fetch_movers(kind: str, provider: str | None = None, n: int = 20) -> pd.DataFrame:
    fn = getattr(obb.equity.discovery, kind)
    resolved_provider = (provider or STREAMLIT_PROVIDER).strip()
    data = fn(provider=resolved_provider) if resolved_provider else fn()
    return data.to_df().head(n)


st.sidebar.header("Scanner Settings")
symbols_raw = st.sidebar.text_area(
    "Symbols (comma-separated)",
    value="AAPL,MSFT,NVDA,TSLA,AMZN,META,GOOGL,AMD,PLTR,SOFI",
    height=100,
)
rsi_min = st.sidebar.number_input("RSI min", min_value=0.0, max_value=100.0, value=40.0)
rsi_max = st.sidebar.number_input("RSI max", min_value=0.0, max_value=100.0, value=70.0)
rvol_min = st.sidebar.number_input("RVOL min", min_value=0.0, value=2.0, step=0.1)
dollar_vol_min_m = st.sidebar.number_input("Dollar volume min (millions)", min_value=0.0, value=50.0)
ma_period = st.sidebar.slider("MA period", min_value=5, max_value=100, value=20)
rsi_period = st.sidebar.slider("RSI period", min_value=5, max_value=30, value=14)
rvol_window = st.sidebar.slider("RVOL window", min_value=5, max_value=60, value=20)

symbols = [s.strip().upper() for s in symbols_raw.split(",") if s.strip()]
if not symbols:
    st.warning("Add at least one symbol in the sidebar.")
    st.stop()

rows = []
errors = []

with st.spinner("Scanning symbols with OpenBB..."):
    for symbol in symbols:
        try:
            df = fetch_history(symbol)
            if df.empty or len(df) < max(ma_period, rsi_period, rvol_window) + 2:
                errors.append(f"{symbol}: not enough data")
                continue

            if "close" not in df.columns or "volume" not in df.columns or "open" not in df.columns:
                errors.append(f"{symbol}: missing close/open/volume columns")
                continue

            close = df["close"]
            volume = df["volume"]
            ma = close.rolling(ma_period).mean()
            rsi = compute_rsi(close, rsi_period)
            avg_vol = volume.rolling(rvol_window).mean().shift(1)

            latest = df.iloc[-1]
            prev = df.iloc[-2]
            latest_close = float(latest["close"])
            latest_open = float(latest["open"])
            latest_volume = float(latest["volume"])
            prev_close = float(prev["close"])

            latest_rsi = float(rsi.iloc[-1]) if pd.notna(rsi.iloc[-1]) else np.nan
            latest_rvol = float(latest_volume / avg_vol.iloc[-1]) if pd.notna(avg_vol.iloc[-1]) and avg_vol.iloc[-1] else np.nan
            latest_dollar_vol = latest_close * latest_volume
            ma_val = float(ma.iloc[-1]) if pd.notna(ma.iloc[-1]) else np.nan
            ma_prox_pct = abs((latest_close - ma_val) / ma_val) * 100 if pd.notna(ma_val) and ma_val else np.nan
            gap_pct = ((latest_open - prev_close) / prev_close) * 100 if prev_close else np.nan

            rows.append(
                {
                    "symbol": symbol,
                    "close": latest_close,
                    "rsi": latest_rsi,
                    "rvol": latest_rvol,
                    "dollar_vol_m": latest_dollar_vol / 1_000_000,
                    "gap_pct": gap_pct,
                    f"ma{ma_period}_prox_pct": ma_prox_pct,
                }
            )
        except Exception as exc:
            errors.append(f"{symbol}: {exc}")

scan_df = pd.DataFrame(rows)
if scan_df.empty:
    st.error("No symbols returned valid data.")
else:
    filtered = scan_df[
        (scan_df["rsi"].between(rsi_min, rsi_max, inclusive="both"))
        & (scan_df["rvol"] >= rvol_min)
        & (scan_df["dollar_vol_m"] >= dollar_vol_min_m)
    ].sort_values(["rvol", "dollar_vol_m"], ascending=False)

    c1, c2, c3 = st.columns(3)
    c1.metric("Universe", len(scan_df))
    c2.metric("Matched", len(filtered))
    c3.metric("Match Rate", f"{(len(filtered) / len(scan_df)) * 100:.1f}%")

    st.subheader("Scanner Results")
    st.dataframe(
        filtered.style.format(
            {
                "close": "{:.2f}",
                "rsi": "{:.2f}",
                "rvol": "{:.2f}",
                "dollar_vol_m": "{:.2f}",
                "gap_pct": "{:.2f}",
                f"ma{ma_period}_prox_pct": "{:.2f}",
            }
        ),
        width="stretch",
    )

    chart_symbol = st.selectbox("Chart Symbol", options=scan_df["symbol"].tolist(), index=0)
    chart_df = fetch_history(chart_symbol)
    fig = go.Figure()
    fig.add_trace(
        go.Candlestick(
            x=chart_df.index,
            open=chart_df["open"],
            high=chart_df["high"],
            low=chart_df["low"],
            close=chart_df["close"],
            name="Price",
        )
    )
    fig.update_layout(height=500, xaxis_rangeslider_visible=False)
    st.plotly_chart(fig, width="stretch")

st.subheader("Market Movers")
col1, col2, col3 = st.columns(3)
with col1:
    st.caption("Top Gainers")
    st.dataframe(fetch_movers("gainers")[["symbol", "price", "percent_change", "volume"]], width="stretch")
with col2:
    st.caption("Top Losers")
    st.dataframe(fetch_movers("losers")[["symbol", "price", "percent_change", "volume"]], width="stretch")
with col3:
    st.caption("Most Active")
    st.dataframe(fetch_movers("active")[["symbol", "price", "percent_change", "volume"]], width="stretch")

if errors:
    with st.expander("Symbol Errors"):
        st.write("\n".join(errors))
