"""
wing_a_consumer_example.py — HOW WING A READS WING B'S SIGNALS.

This file is documentation-as-code: it is the piece that lives on
the WING A side (inside the alpha model service), shown here so you
can see both ends of the bridge in one project.

Two independent mechanisms, doing different jobs:

  1. read_analyst_signals()  — pull: query the shared DB on every
     signal-compute cycle. Reliable, simple, the source of truth.

  2. listen_for_signals()    — push: subscribe to the Redis channel
     so a brand-new signal triggers an IMMEDIATE re-score instead
     of waiting for the next scheduled cycle.

Wing A uses BOTH: Redis for speed, the DB for truth.
"""

from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import create_engine, text

# In Wing A this URL comes from ITS config — but it points at the
# SAME database Wing B writes to. Shared DB = the integration point.
DATABASE_URL = "sqlite:///wing_b_local.db"          # demo value
engine = create_engine(DATABASE_URL, future=True)

# How much weight analyst views get in the blended score.
# 0.0 = ignore analysts entirely (purely systematic)
# 1.0 = analyst views dominate
# This being a CONFIG VALUE, not code, is the point: you can tune
# the quantamental blend without retraining anything.
ANALYST_WEIGHT = 0.3


def read_analyst_signals() -> pd.DataFrame:
    """Pull all non-expired analyst signals from the shared table."""
    return pd.read_sql(
        text("""
            SELECT ticker, direction, conviction
            FROM analyst_signals
            WHERE expires_at > :now        -- ignore stale ideas
        """),
        engine,
        params={"now": datetime.now(timezone.utc)},
    )


def analyst_score(ticker: str, signals: pd.DataFrame) -> float:
    """Convert an analyst signal into a number on the SAME -1..+1
    scale the systematic signals use, so they can be blended.

    long  conviction 5 → +1.0      short conviction 5 → -1.0
    long  conviction 1 → +0.2      no signal          →  0.0
    """
    row = signals[signals["ticker"] == ticker]
    if row.empty:
        return 0.0                                   # no analyst view

    direction = row.iloc[0]["direction"]             # 'long' or 'short'
    conviction = row.iloc[0]["conviction"]           # 1..5
    sign = 1.0 if direction == "long" else -1.0
    return sign * conviction / 5.0                   # scale to ±1


def blended_signal(ticker: str, systematic: float,
                   signals: pd.DataFrame) -> float:
    """The quantamental blend — one weighted average.

    final = (1 - w) * model_view + w * analyst_view
    """
    human = analyst_score(ticker, signals)
    return (1 - ANALYST_WEIGHT) * systematic + ANALYST_WEIGHT * human


def listen_for_signals() -> None:
    """OPTIONAL push path: block on the Redis channel and react the
    moment Wing B publishes. Runs in its own thread inside Wing A."""
    import redis

    r = redis.Redis(host="localhost", port=6379)
    pubsub = r.pubsub()                      # a subscription handle
    pubsub.subscribe("analyst_signals")      # the channel Wing B publishes on

    print("[wing-a] listening for analyst signals...")
    # .listen() yields every message as it arrives — an infinite loop
    # by design; this function never returns.
    for message in pubsub.listen():
        if message["type"] == "message":     # skip subscribe confirmations
            payload = message["data"].decode()        # bytes → str, e.g. "AAPL:long:4"
            print(f"[wing-a] new analyst signal: {payload} — re-scoring now")
            # ...here the real Wing A would re-run blended_signal()
            # for the affected ticker and push updated orders.


# Tiny demo when run directly: blend a fake systematic score with
# whatever signals are currently in the shared database.
if __name__ == "__main__":
    sigs = read_analyst_signals()
    print(f"{len(sigs)} active analyst signal(s) found")
    demo = blended_signal("AAPL", systematic=0.4, signals=sigs)
    print(f"AAPL blended signal: {demo:+.3f}  (weight={ANALYST_WEIGHT})")
