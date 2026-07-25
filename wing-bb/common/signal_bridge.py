"""
common/signal_bridge.py — converts analyst_signals rows into the SAME
S3 contract the ML scorer uses, so both flow through Wing A's one
ingestor (Option A: unified pipeline).

The analyst table stores a HUMAN view (direction + conviction + thesis).
Wing A's ingestor expects a QUANTITATIVE signal. This module does the
translation, then hands off to common/signal_writer.py to emit it.

Field mapping (analyst_signals  ->  ingestor contract):
  ticker               ->  symbol
  direction+conviction ->  signal          (long c5 = +1.0, short c5 = -1.0)
  conviction           ->  confidence      (lookup table)
  conviction           ->  target_qty      (lookup table)
  (none)               ->  limit_price_bps = 0  (market — analysts set no price)
  created_at           ->  generated_at
  thesis, expires_at   ->  dropped (not in the contract; stay in the DB
                           for audit — the ingestor never needs them)
"""

from common.signal_writer import build_signal_dict, write_signal


# conviction 1..5  ->  model confidence 0..1
# 5 (table-pounding) = high certainty; 1 (weak) = low.
CONVICTION_TO_CONFIDENCE = {1: 0.30, 2: 0.45, 3: 0.60, 4: 0.80, 5: 0.95}

# conviction 1..5  ->  suggested position size (shares)
# Tune to match your risk engine's appetite.
CONVICTION_TO_QTY = {1: 100, 2: 200, 3: 300, 4: 400, 5: 500}


def analyst_row_to_signal_dict(row: dict) -> dict:
    """Convert ONE analyst_signals row (as a dict) into the ingestor
    contract dict. Does NOT write anything — pure transformation,
    easy to unit-test."""
    conviction = int(row["conviction"])

    # direction + conviction -> one signed, normalised float in [-1, 1]
    sign = 1.0 if row["direction"] == "long" else -1.0
    signal = sign * (conviction / 5.0)
    # long c5 -> +1.0 | long c3 -> +0.6 | short c4 -> -0.8 | short c1 -> -0.2

    return build_signal_dict(
        symbol=row["ticker"],
        signal=signal,
        confidence=CONVICTION_TO_CONFIDENCE[conviction],
        target_qty=CONVICTION_TO_QTY[conviction],
        limit_price_bps=0,                       # market order
        generated_at=row.get("created_at"),      # datetime or None
    )


def publish_analyst_signal(row: dict) -> str:
    """Full path: analyst row -> contract dict -> written to S3 under
    the 'analyst/' source folder. Returns the key/path written.

    Called by the Streamlit publisher page right after it INSERTs the
    row into analyst_signals."""
    signal_dict = analyst_row_to_signal_dict(row)
    return write_signal(signal_dict, source="analyst")
