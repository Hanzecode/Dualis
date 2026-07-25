"""
2_stock_screener.py — filter the universe by fundamental criteria.

WHAT THE ANALYST SEES: sliders for P/E, growth, margin etc.; the
table below updates live to show only stocks passing every filter.
This is the no-code version of 'SELECT * FROM stocks WHERE ...'.
"""

import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parents[2]))

import pandas as pd
import streamlit as st

from common.db import engine

st.title("Stock screener")

@st.cache_data
def load_fundamentals() -> pd.DataFrame:
    return pd.read_sql("SELECT * FROM sector_fundamentals", engine)

df = load_fundamentals()
if df.empty:
    st.warning("No data — run `python run_etl.py` first.")
    st.stop()

# ── Sidebar controls ───────────────────────────────────────────────
# Everything created inside `with st.sidebar:` renders in the left
# panel instead of the main page — keeps filters separate from results.
with st.sidebar:
    st.header("Filters")

    # st.slider(label, min, max, default) returns the CURRENT value.
    # When the user drags it, Streamlit reruns this script and the
    # variable simply holds the new number — that's the reactive model.
    max_pe = st.slider(
        "Max P/E",
        5.0,                                # slider minimum
        50.0,                               # slider maximum
        45.0,                               # starting value
    )

    min_growth = st.slider("Min revenue growth", -0.10, 0.30, -0.05, format="%.2f")
    min_margin = st.slider("Min profit margin",   0.00, 0.40,  0.00, format="%.2f")

    # st.multiselect returns a LIST of the chosen options.
    # df["sector"].unique() supplies the available choices;
    # default=... pre-selects all of them.
    sectors = st.multiselect(
        "Sectors",
        options=sorted(df["sector"].unique()),
        default=sorted(df["sector"].unique()),
    )

# ── Apply every filter ─────────────────────────────────────────────
# Each comparison creates a True/False mask; & combines them.
# Parentheses around each condition are REQUIRED in pandas.
mask = (
    (df["pe_ratio"]   <= max_pe)
    & (df["rev_growth"] >= min_growth)
    & (df["margin"]     >= min_margin)
    & (df["sector"].isin(sectors))        # .isin = 'value in this list'
)
result = df[mask].sort_values("momentum_6m", ascending=False)

# Show how many survived the filters.
st.subheader(f"{len(result)} stocks pass the filters")

# The results table, nicely formatted per column.
st.dataframe(
    result.style.format({
        "pe_ratio":    "{:.1f}",
        "rev_growth":  "{:+.1%}",
        "margin":      "{:.1%}",
        "momentum_6m": "{:+.1%}",
        "volatility":  "{:.2%}",
    }),
    use_container_width=True,
)

# ── CSV export ─────────────────────────────────────────────────────
# to_csv(index=False) turns the frame into CSV text;
# st.download_button offers it as a file download.
st.download_button(
    "Download as CSV",
    data=result.to_csv(index=False),
    file_name="screener_results.csv",
    mime="text/csv",
)
