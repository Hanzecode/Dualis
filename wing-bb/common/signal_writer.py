"""
common/signal_writer.py — writes signals to S3 in the EXACT format
Wing A's ingestor.py expects.

This is the single shared 'emit' path. BOTH producers use it:
  - the ML scorer  (scoring/scorer.py)
  - the analyst publisher (dashboard page 5, via signal_bridge.py)

That's the whole point of Option A: one contract, one writer, one
S3 prefix. Wing A's ingestor polls that prefix and cannot tell (or
care) whether a file came from a model or a human — it just
validates the JSON and emits an AlphaSignal.

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


# Where signals go. In production these are the SAME bucket/prefix the
# ingestor polls (config.s3.signal_bucket / signal_prefix).
S3_BUCKET = os.getenv("SIGNAL_BUCKET", "hedgefund-signals")
S3_PREFIX = os.getenv("SIGNAL_PREFIX", "signals/")

# Local fallback dir — used when AWS isn't configured, so the whole
# thing runs on your laptop. Mirrors the S3 prefix as a folder.
LOCAL_SIGNAL_DIR = os.getenv("LOCAL_SIGNAL_DIR", "signals_out")


def _iso_utc(dt: datetime | None = None) -> str:
    """Return a UTC ISO8601 string with the 'Z' suffix the ingestor
    parses. The ingestor does .replace('Z', '+00:00'), so 'Z' is
    exactly what it expects."""
    dt = dt or datetime.now(timezone.utc)
    # isoformat() on an aware UTC datetime gives '...+00:00';
    # swap that for 'Z' to match the contract precisely.
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
    by both producers without touching S3.
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
    """Write ONE signal to S3 (or the local fallback).

    source ('model' or 'analyst') is encoded in the S3 KEY PATH, not
    the JSON body — the body must stay exactly the contract. This
    gives you provenance/audit ('which folder did it come from?')
    without polluting the schema the ingestor validates.

    Returns the S3 key (or local path) written.
    """
    # One file per signal — the ingestor lists objects and treats each
    # as a single signal. A unique name prevents collisions and lets
    # the ingestor's _seen_keys dedup work correctly.
    symbol = signal_dict["symbol"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    unique = uuid.uuid4().hex[:8]                       # avoid same-second clashes
    key = f"{S3_PREFIX}{source}/{symbol}_{stamp}_{unique}.json"

    body = json.dumps(signal_dict, indent=2)

    # ── Try real S3 first ─────────────────────────────────────────
    try:
        import boto3
        s3 = boto3.client("s3")
        s3.put_object(Bucket=S3_BUCKET, Key=key, Body=body.encode("utf-8"))
        return f"s3://{S3_BUCKET}/{key}"
    except Exception as exc:  # noqa: BLE001 — no AWS creds / no boto3 / offline
        # ── Local fallback: write the same file to disk ───────────
        # Same key path under a local folder, so you can inspect the
        # exact JSON the ingestor would receive.
        local_path = Path(LOCAL_SIGNAL_DIR) / key
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(body)
        return str(local_path)


def write_signals(signal_dicts: list[dict], source: str = "model") -> list[str]:
    """Write many signals at once. Returns the list of keys/paths."""
    return [write_signal(s, source=source) for s in signal_dicts]
