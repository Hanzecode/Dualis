"""
1_sector_overview.py — heatmap-style view of how each sector is doing.

WHAT THE ANALYST SEES: each sector's return over several horizons
(1 day, 1 week, 1 month, 6 months) so they can spot rotation —
money moving out of one sector and into another.
"""

import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parents[2]))  # project root on the path

import pandas as pd
import streamlit as st

from common.db import engine

st.title("Sector overview")

# ── Macro panel — the top-down context ─────────────────────────────
# Latest reading of each FRED series, with the change vs ~1 month ago
# (21 trading days) as the delta arrow. Rates up / VIX up usually
# pressure equities — analysts want this context BEFORE sector moves.
@st.cache_data
def load_macro() -> pd.DataFrame:
    try:
        return pd.read_sql(
            "SELECT obs_date, series_id, series_name, value FROM macro_indicators",
            engine,
        )
    except Exception:               # table missing (old DB) → empty frame
        return pd.DataFrame()

macro = load_macro()
if not macro.empty:
    st.subheader("Macro indicators")
    # One st.metric per series, laid out in a row of columns.
    cols = st.columns(macro["series_id"].nunique())
    for col, (sid, grp) in zip(cols, macro.groupby("series_id")):
        grp = grp.sort_values("obs_date")
        latest = grp["value"].iloc[-1]                  # newest reading
        month_ago = grp["value"].iloc[-21] if len(grp) > 21 else latest
        with col:
            st.metric(
                grp["series_name"].iloc[0],
                f"{latest:,.2f}",
                delta=f"{latest - month_ago:+,.2f} (1m)",
            )
    st.divider()


# ── @st.cache_data: THE key Streamlit decorator ────────────────────
# Streamlit reruns this whole script on EVERY user interaction.
# Without caching, every click would re-query the database.
# With it, the function runs once, the result is stored, and
# subsequent reruns reuse the stored result instantly.
@st.cache_data
def load_prices() -> pd.DataFrame:
    return pd.read_sql(
        "SELECT trade_date, ticker, sector, close FROM sector_prices",
        engine,
    )

prices = load_prices()

# Guard clause: stop the page cleanly if ETL hasn't run yet.
if prices.empty:
    st.warning("No data — run `python run_etl.py` first.")
    st.stop()    # halts the script here; nothing below renders


@st.cache_data
def calculate_sector_performance(df: pd.DataFrame, days: int = 126) -> pd.DataFrame:
    """Calculate cumulative return for each sector over the last `days`.

    Returns a DataFrame with trade_date as index and one column per sector,
    showing cumulative performance.
    """
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.sort_values(["ticker", "trade_date"])
    df["daily_ret"] = df.groupby("ticker")["close"].pct_change().fillna(0)

    # Equal-weight daily return for each sector
    sector_daily_returns = df.groupby(["trade_date", "sector"])["daily_ret"].mean().unstack()

    # Limit to the last `days` and calculate cumulative performance
    performance = (1 + sector_daily_returns.tail(days)).cumprod() - 1
    return performance


st.subheader("Daily Cumulative Sector Performance (6 Months)")
performance_chart_data = calculate_sector_performance(prices, days=126)

st.line_chart(performance_chart_data)

st.caption(
    "Performance is compounded daily for each sector over the last 6 months (126 trading days). "
    "This high-resolution view helps visualize trends and rotation between sectors over time."
)
