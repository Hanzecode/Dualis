"""
app.py — the Streamlit home page (the analyst's front door).

HOW STREAMLIT MULTI-PAGE APPS WORK:
This file is the landing page. Every .py file inside the pages/
folder automatically becomes a page in the left sidebar — the
number prefix (1_, 2_, ...) controls the order. No routing code
needed; the folder structure IS the navigation.

Run it with:   streamlit run dashboard/app.py
"""

import streamlit as st

# Make project-root imports (common.*, etl.*, analytics.*) work no
# matter where streamlit is launched from: add the parent folder of
# dashboard/ to Python's import search path.
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parents[1]))

from common.db import engine, init_tables

# ── Page configuration — must be the FIRST Streamlit call ─────────
st.set_page_config(
    page_title="Wing B — Analyst Platform",
    layout="wide",                 # use the full browser width
)

# st.title / st.markdown render text. Markdown formatting works.
st.title("Wing B — sector analyst platform")
st.markdown(
    "Use the sidebar to navigate: sector overview, screener, "
    "factor explorer, valuation model, and the signal publisher "
    "that feeds ideas back into Wing A."
)

# ── First-run check: is there any data yet? ────────────────────────
init_tables()   # create tables if missing (safe to re-run)

# Try counting rows in the prices table.
try:
    import pandas as pd
    n = pd.read_sql("SELECT COUNT(*) AS n FROM sector_prices", engine)["n"][0]
except Exception:
    n = 0

if n == 0:
    # st.warning shows a yellow callout box.
    st.warning(
        "No data found. Run the ETL first:\n\n"
        "```bash\npython run_etl.py\n```"
    )
else:
    # st.success shows a green callout; f-string injects the number.
    st.success(f"Database ready — {n:,} price rows loaded.")

    # st.metric shows a big number with a label — good for dashboards.
    col1, col2 = st.columns(2)          # split the row into 2 columns
    with col1:
        st.metric("Price rows", f"{n:,}")
    with col2:
        funda = pd.read_sql("SELECT COUNT(*) AS n FROM sector_fundamentals", engine)["n"][0]
        st.metric("Stocks tracked", funda)

    st.divider()
    st.subheader("Latest prices")

    # Fetch and display the most recent day's prices.
    # Using a subquery to find the max date is efficient.
    latest_prices = pd.read_sql(
        "SELECT * FROM sector_prices "
        "WHERE trade_date = (SELECT MAX(trade_date) FROM sector_prices) "
        "ORDER BY ticker",
        engine,
    )
    st.dataframe(
        latest_prices.style.format({
            "close": "${:,.2f}",
            "volume": "{:,.0f}",
            "daily_ret": "{:+.2%}",
        }),
        use_container_width=True,
    )
