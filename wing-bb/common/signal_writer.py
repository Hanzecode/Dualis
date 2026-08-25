"""
common/signal_writer.py — writes signals to local disk in the EXACT format
Wing A's ingestor.py expects.

This is the single shared 'emit' path. BOTH producers use it:
  - the ML scorer  (scoring/scorer.py)
  - the analyst publisher (dashboard page 5, via signal_bridge.py)

Wing A's ingestor polls this directory and validates the JSON and emits an AlphaSignal.

CONTRACT (must match ingestor.py exactly):
  {
    "symbol":          "AAPL",      # uppercase, must be in universe
    "signal":          0.73,        # float in [-1.0, +1.0]
    "confidence":      0.85,        # float in [0.0, 1.0]
    "target_qty":      500,         # int > 0
    "limit_price_bps": 0,           # int, 0 = market order
    "generated_at":    "2024-01-15T09:30:00Z"  # UTC ISO8601 with Z
  }
"""

import os
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path


# Directory where signals are stored on disk.
LOCAL_SIGNAL_DIR = os.getenv("LOCAL_SIGNAL_DIR", "signals_out")


def _iso_utc(dt: datetime | None = None) -> str:
    """Return a UTC ISO8601 string with the 'Z' suffix the ingestor
    parses. The ingestor does .replace('Z', '+00:00'), so 'Z' is
    exactly what it expects."""
    dt = dt or datetime.now(timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def build_signal_dict(
    symbol: str,
    signal: float,
    confidence: float,
    target_qty: int,
    limit_price_bps: int = 0,
    generated_at: datetime | None = None,
) -> dict:
    """Assemble ONE signal dict in the ingestor's contract.

    Kept separate from the writing so it can be unit-tested and reused
    by both producers without touching disk.
    """
    return {
        "symbol":          str(symbol).upper(),   # ingestor uppercases too, but be safe
        "signal":          round(float(signal), 4),
        "confidence":      round(float(confidence), 4),
        "target_qty":      int(target_qty),
        "limit_price_bps": int(limit_price_bps),
        "generated_at":    _iso_utc(generated_at),
    }


def write_signal(signal_dict: dict, source: str = "model") -> str:
    """Write ONE signal to local disk.

    source ('model' or 'analyst') is encoded in the folder path, not
    the JSON body — the body must stay exactly the contract.

    Returns the local path written.
    """
    symbol = signal_dict["symbol"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    unique = uuid.uuid4().hex[:8]                       # avoid same-second clashes
    file_rel = f"signals/{source}/{symbol}_{stamp}_{unique}.json"

    body = json.dumps(signal_dict, indent=2)

    local_path = Path(LOCAL_SIGNAL_DIR) / file_rel
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text(body, encoding="utf-8")
    return str(local_path)


def write_signals(signal_dicts: list[dict], source: str = "model") -> list[str]:
    """Write many signals at once. Returns the list of paths."""
    return [write_signal(s, source=source) for s in signal_dicts]
