"""
5_signal_publisher.py — THE BRIDGE TO WING A.

This page is the whole reason the two-wing design exists. When an
analyst has a high-conviction view, they fill in this form and two
things happen:

  1. A row is INSERTed into the shared analyst_signals table.
     Wing A's alpha model SELECTs from that table on every compute
     cycle and blends the analyst view into its systematic score.

  2. A message is PUBLISHed on a Redis channel, so Wing A learns
     about the new signal INSTANTLY instead of waiting for its next
     scheduled database read.

Database = the durable record. Redis = the instant doorbell.
"""

import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parents[2]))

from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st
from sqlalchemy import text

from common.db import engine
from common.config import REDIS_HOST, REDIS_PORT
from common.signal_bridge import publish_analyst_signal


st.title("Signal publisher")
st.markdown("Publish a trade idea into the systematic pipeline (Wing A).")

# ── The input form ─────────────────────────────────────────────────
# st.form batches the widgets: nothing is submitted (and the script
# isn't re-run) until the user clicks the submit button. Without a
# form, every keystroke in the thesis box would trigger a rerun.
with st.form("signal_form"):

    # Load tickers fresh each render so the dropdown matches the DB.
    tickers = pd.read_sql(
        "SELECT ticker FROM sector_fundamentals ORDER BY ticker", engine
    )["ticker"].tolist()

    ticker = st.selectbox("Ticker", tickers)

    # st.radio renders mutually-exclusive choices; horizontal=True
    # puts them side by side instead of stacked.
    direction = st.radio("Direction", ["long", "short"], horizontal=True)

    # Conviction 1–5: how strongly the analyst believes the idea.
    # Wing A can weight stronger convictions more heavily.
    conviction = st.slider("Conviction (1 = weak, 5 = table-pounding)", 1, 5, 3)

    # Free-text reasoning — the human context a pure model never has.
    thesis = st.text_area(
        "Thesis",
        placeholder="e.g. Regulatory headwinds will compress margins across EU banks...",
    )

    # How long the idea stays live before Wing A ignores it.
    horizon_days = st.number_input("Valid for (days)", 1, 90, 14)

    # The submit button. Returns True only on the click that submits.
    submitted = st.form_submit_button("Publish to Wing A")

# ── On submit: write to DB, then ping Redis ────────────────────────
if submitted:
    if not thesis.strip():                  # require a real thesis
        st.error("Please write a thesis — Wing A logs it for audit.")
        st.stop()

    now = datetime.now(timezone.utc)        # store times in UTC, always

    # STEP 1 — durable write into the SHARED database.
    # :named placeholders + a parameters dict = safe parameterised
    # SQL. NEVER build SQL with f-strings — that's how SQL injection
    # happens.
    with engine.begin() as conn:             # transaction: all-or-nothing
        conn.execute(
            text("""
                INSERT INTO analyst_signals
                    (created_at, ticker, direction, conviction, thesis, expires_at)
                VALUES
                    (:created_at, :ticker, :direction, :conviction, :thesis, :expires_at)
            """),
            {
                "created_at": now,
                "ticker":     ticker,
                "direction":  direction,
                "conviction": int(conviction),
                "thesis":     thesis.strip(),
                "expires_at": now + timedelta(days=int(horizon_days)),
            },
        )

    try:
        s3_path = publish_analyst_signal({
            "ticker":     ticker,
            "direction":  direction,
            "conviction": int(conviction),
            "created_at": now,
        })
        st.caption(f"Signal written to Wing A pipeline: {s3_path}")
    except Exception as exc:
        # The signal IS safely in the DB. S3 failing is not fatal —
        # log it and tell the user, but don't lose their signal.
        st.warning(f"Saved to database, but S3 write failed: {exc}")

    # STEP 2 — instant notification via Redis pub/sub (best effort).
    # If Redis isn't running (local dev), we just skip the doorbell;
    # Wing A will still pick the signal up on its next DB read.
    try:
        import redis                                       # optional dependency
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, socket_connect_timeout=1)
        # publish(channel, message): every subscriber to the
        # 'analyst_signals' channel receives this string immediately.
        r.publish("analyst_signals", f"{ticker}:{direction}:{conviction}")
        st.success(f"Published {direction.upper()} {ticker} — Wing A notified via Redis.")
    except Exception:
        st.success(f"Published {direction.upper()} {ticker} — saved to DB "
                   "(Redis not reachable; Wing A will pick it up on its next cycle).")

st.divider()

# ── Live signals table ─────────────────────────────────────────────
st.subheader("Active signals")
active = pd.read_sql(
    # Only signals that haven't expired yet, newest first.      
    "SELECT created_at, ticker, direction, conviction, thesis, expires_at "
    "FROM analyst_signals "
    "ORDER BY created_at DESC",
    engine,
)
if active.empty:
    st.info("No signals published yet.")
else:
    st.dataframe(active, use_container_width=True)
