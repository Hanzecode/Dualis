"""
3_factor_explorer.py — visualise each sector's factor exposures.

WHAT THE ANALYST SEES: for every sector, its beta to the market
factor, its beta to momentum, and its unexplained alpha — answering
'WHY is this sector moving?' rather than just 'IS it moving?'.
Also shows the fundamental clusters from the clustering module.
"""

import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parents[2]))

import streamlit as st

from analytics.factor_model import sector_exposures
from analytics.clustering import cluster_stocks

st.title("Factor explorer")

# ── Factor exposures table + chart ─────────────────────────────────
# cache_data memoises the regression — it only re-runs if the
# underlying code or inputs change, not on every page interaction.
@st.cache_data
def get_exposures():
    return sector_exposures()

expo = get_exposures()

st.subheader("Sector factor exposures")
st.dataframe(expo, use_container_width=True)

# st.bar_chart is Streamlit's one-liner chart. set_index makes the
# sector names the x-axis labels; the remaining columns become bars.
st.bar_chart(expo.set_index("sector")[["market_beta", "momentum_beta"]])

st.caption(
    "market_beta > 1 means the sector amplifies market moves; "
    "momentum_beta > 0 means it tilts toward recent winners. "
    "alpha_annual is the return the factors can't explain."
)

# st.divider draws a horizontal rule between the two sections.
st.divider()

# ── Fundamental clusters ───────────────────────────────────────────
st.subheader("Fundamental clusters")

# Let the analyst choose how many groups to form.
k = st.slider("Number of clusters", 2, 5, 3)

@st.cache_data
def get_clusters(n: int):
    # The slider value is part of the cache key: k=3 and k=4 cache
    # separately, so flipping back and forth is instant.
    return cluster_stocks(n_clusters=n)

clustered = get_clusters(k)

# Scatter plot in PCA space; color encodes cluster membership.
st.scatter_chart(
    clustered,
    x="pca_x",
    y="pca_y",
    color="cluster",
    size="pe_ratio",          # bubble size = how expensive the stock is
)

st.caption(
    "Stocks close together have similar fundamentals regardless of "
    "sector label. Same cluster + very different P/E = a relative "
    "value candidate worth investigating."
)

# Show the membership table under the chart.
st.dataframe(
    clustered[["ticker", "sector", "cluster", "pe_ratio", "rev_growth", "margin"]]
        .sort_values("cluster"),
    use_container_width=True,
)
