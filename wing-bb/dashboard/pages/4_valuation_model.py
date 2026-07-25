"""
4_valuation_model.py — the ML fair-value estimate, with SHAP receipts.

WHAT THE ANALYST SEES: every stock ranked by 'upside' (how far the
model thinks it is from fair value), and for any chosen stock, a
breakdown of WHICH fundamentals drove the model's estimate — the
explainability that makes the number trustworthy.
"""

import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parents[2]))

import streamlit as st

from analytics.valuation import train_and_score, shap_explanation

st.title("Valuation model")

# ── @st.cache_resource vs @st.cache_data ───────────────────────────
# cache_data    → for DATA (DataFrames, lists). Stored by value.
# cache_resource→ for heavyweight OBJECTS like trained models or DB
#                 connections that shouldn't be copied — stored once
#                 and shared. A trained model is exactly that.
@st.cache_resource
def get_model():
    return train_and_score()      # returns (scored_dataframe, model)

scored, model = get_model()

# ── The ranking table ──────────────────────────────────────────────
st.subheader("Upside vs model fair value")
st.dataframe(
    scored[["ticker", "sector", "pe_ratio", "fair_pe", "upside"]]
        .style.format({
            "pe_ratio": "{:.1f}",
            "fair_pe":  "{:.1f}",
            "upside":   "{:+.1%}",
        })
        # Green bar for positive upside, red for negative — a quick
        # visual scan of where the opportunities might be.
        .bar(subset=["upside"], align="zero", color=["#c33", "#2a7"]),
    use_container_width=True,
)

st.caption(
    "upside = fair_pe / actual_pe − 1. Positive means the stock trades "
    "BELOW what its fundamentals justify according to the model. "
    "A model view, not investment advice — the next panel shows WHY."
)

st.divider()

# ── SHAP: explain one stock ────────────────────────────────────────
st.subheader("Why? — SHAP breakdown for one stock")

# selectbox = dropdown. Returns the chosen ticker string.
ticker = st.selectbox("Pick a stock", scored["ticker"].tolist())

try:
    shap_df = shap_explanation(model, scored, ticker)

    # Horizontal-ish bar chart of each feature's push on the estimate.
    st.bar_chart(shap_df.set_index("feature")["shap_value"])

    st.caption(
        "Positive bars pushed this stock's fair P/E ABOVE the average "
        "stock's; negative bars pulled it below. The model's reasoning, "
        "itemised — this is what makes an analyst trust (or challenge) it."
    )
except ImportError:
    # shap is an optional dependency; degrade gracefully if missing.
    st.info("Install the 'shap' package to see per-stock explanations: pip install shap")
