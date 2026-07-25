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


def sector_return(df: pd.DataFrame, days: int) -> pd.Series:
    """Average % return of each sector over the last `days` rows.

    Method: for each ticker take price_now / price_n_days_ago - 1,
    then average those returns within each sector.
    """
    out = {}
    for (sector, ticker), grp in df.groupby(["sector", "ticker"]):
        grp = grp.sort_values("trade_date")
        if len(grp) > days:                       # enough history?
            ret = grp["close"].iloc[-1] / grp["close"].iloc[-1 - days] - 1
            out.setdefault(sector, []).append(ret)  # collect per sector
    # Average the per-ticker returns inside each sector.
    return pd.Series({s: sum(v) / len(v) for s, v in out.items()})


# Build one column per horizon. dict-comprehension keeps it tidy.
horizons = {"1 day": 1, "1 week": 5, "1 month": 21, "6 months": 126}
table = pd.DataFrame({label: sector_return(prices, d) for label, d in horizons.items()})

# Style: format as percentages and color negative red / positive green.
# .style.format applies display formatting without changing the data.
st.dataframe(
    table.style
         .format("{:+.2%}")                       # e.g. +3.41%
         .map(lambda v: "color: #c33" if v < 0 else "color: #2a7"),
    use_container_width=True,
)

st.caption(
    "Read across a row to see one sector's performance at different "
    "horizons. A sector strong at 6 months but weak at 1 week may be "
    "starting to roll over — classic rotation signal."
)
