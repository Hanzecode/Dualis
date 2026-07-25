# wing_a/signal_ingestion/ingestor.py
# ─────────────────────────────────────────────────────────────────────────────
#  SIGNAL INGESTOR
#  Polls S3 every N seconds for signal files written by Wing B's score DAG.
#  Validates each signal (schema, staleness, universe membership).
#  Emits validated AlphaSignal dataclasses to a callback.
#
#  Flow:  S3 bucket (Wing B writes) → poll → validate → AlphaSignal callback
#
#  Message contract with Wing B score DAG (JSON in S3):
#  {
#    "symbol":          "AAPL",
#    "signal":          0.73,          # normalised alpha [-1.0, +1.0]
#    "confidence":      0.85,          # model confidence [0.0, 1.0]
#    "target_qty":      500,           # suggested position size (shares)
#    "limit_price_bps": 1890500,       # suggested limit price (0 = market)
#    "generated_at":    "2024-01-15T09:30:00Z"   # UTC ISO8601
#  }
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, List, Optional, Set

import boto3                     # AWS SDK — pip install boto3
from botocore.exceptions import ClientError

from config.settings import Settings, settings as default_settings

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  ALPHA SIGNAL DATACLASS
#  This is the internal representation after parsing and validation.
#  Passed downstream to PortfolioConstructor and RiskGate.
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)   # frozen=True: immutable after creation — safe to pass between threads
class AlphaSignal:
    symbol:          str
    signal:          float    # [-1.0, +1.0] — positive = buy, negative = sell
    confidence:      float    # [0.0, 1.0] — model certainty
    target_qty:      int      # absolute target position size (shares)
    limit_price_bps: int      # 0 = use market order
    generated_at:    datetime # UTC timestamp from Wing B

    @property
    def is_buy(self) -> bool:
        return self.signal > 0

    @property
    def is_sell(self) -> bool:
        return self.signal < 0

    @property
    def limit_price_dollars(self) -> float:
        # Convenience: convert bps back to dollars for logging
        return self.limit_price_bps / 10_000.0


# ─────────────────────────────────────────────────────────────────────────────
#  SIGNAL INGESTOR
# ─────────────────────────────────────────────────────────────────────────────

class SignalIngestor:
    """
    Runs a background thread that polls S3 every poll_interval_s seconds.
    On each poll, lists new signal files, downloads and parses each one,
    validates it, and calls on_signal(AlphaSignal) for each valid signal.

    Thread model: one daemon thread runs _poll_loop().
    The on_signal callback is called from that thread — make it thread-safe.
    """

    def __init__(
        self,
        on_signal: Callable[[AlphaSignal], None],
        cfg: Settings = default_settings,
    ):
        self._on_signal = on_signal
        self._cfg = cfg
        self._s3 = boto3.client("s3")   # Uses ~/.aws/credentials or IAM role

        # Track which files we've already processed so we don't re-emit on each poll
        # In production: persist this set to DynamoDB or a file so it survives restarts
        self._seen_keys: Set[str] = set()

        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="signal-ingestor",
            daemon=True,    # daemon=True: thread dies when main thread exits
        )

    def start(self) -> None:
        logger.info("SignalIngestor starting — bucket=%s prefix=%s poll=%ds",
                    self._cfg.s3.signal_bucket,
                    self._cfg.s3.signal_prefix,
                    self._cfg.s3.poll_interval_s)
        self._thread.start()

    def stop(self) -> None:
        logger.info("SignalIngestor stopping")
        self._stop_event.set()        # Signal the loop to exit
        self._thread.join(timeout=5)  # Wait up to 5s for clean exit

    # ── Private ───────────────────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        """Main loop: poll S3, process new files, sleep, repeat."""
        while not self._stop_event.is_set():
            try:
                self._poll_once()
            except Exception as e:
                # Never crash the polling thread on a transient error
                logger.error("SignalIngestor poll error: %s", e, exc_info=True)

            # Wait for poll_interval_s, but wake immediately if stop() is called
            self._stop_event.wait(timeout=self._cfg.s3.poll_interval_s)

    def _poll_once(self) -> None:
        """List S3 prefix, download new files, validate, emit."""
        try:
            # list_objects_v2: paginated listing of all objects under the prefix
            response = self._s3.list_objects_v2(
                Bucket=self._cfg.s3.signal_bucket,
                Prefix=self._cfg.s3.signal_prefix,
            )
        except ClientError as e:
            logger.error("S3 list_objects failed: %s", e)
            return

        objects = response.get("Contents", [])
        if not objects:
            logger.debug("No signal files found in S3")
            return

        new_count = 0
        for obj in objects:
            key = obj["Key"]
            if key in self._seen_keys:
                continue   # Already processed

            signal = self._download_and_parse(key)
            if signal is not None:
                self._on_signal(signal)   # Emit to downstream (portfolio constructor)
                new_count += 1

            self._seen_keys.add(key)   # Mark as seen regardless — don't retry bad files

        if new_count > 0:
            logger.info("SignalIngestor emitted %d new signals", new_count)

    def _download_and_parse(self, key: str) -> Optional[AlphaSignal]:
        """Download one S3 object and parse it into an AlphaSignal."""
        try:
            obj = self._s3.get_object(
                Bucket=self._cfg.s3.signal_bucket,
                Key=key,
            )
            raw = obj["Body"].read().decode("utf-8")
        except ClientError as e:
            logger.error("S3 get_object failed for %s: %s", key, e)
            return None

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning("Malformed JSON in %s: %s", key, e)
            return None

        return self._validate(data, key)

    def _validate(self, data: dict, key: str) -> Optional[AlphaSignal]:
        """
        Validate schema, staleness, and universe membership.
        Returns None (and logs why) if the signal should be rejected.
        """
        # ── Schema check ───────────────────────────────────────────────────
        required = {"symbol", "signal", "confidence", "target_qty",
                    "limit_price_bps", "generated_at"}
        missing = required - data.keys()
        if missing:
            logger.warning("Signal %s missing fields: %s", key, missing)
            return None

        # ── Type coercion ───────────────────────────────────────────────────
        try:
            symbol          = str(data["symbol"]).upper()
            signal          = float(data["signal"])
            confidence      = float(data["confidence"])
            target_qty      = int(data["target_qty"])
            limit_price_bps = int(data["limit_price_bps"])
            generated_at    = datetime.fromisoformat(
                data["generated_at"].replace("Z", "+00:00")
            )
        except (ValueError, TypeError) as e:
            logger.warning("Signal %s type error: %s", key, e)
            return None

        # ── Range checks ───────────────────────────────────────────────────
        if not (-1.0 <= signal <= 1.0):
            logger.warning("Signal %s out of range: signal=%.3f", key, signal)
            return None
        if not (0.0 <= confidence <= 1.0):
            logger.warning("Signal %s confidence out of range: %.3f", key, confidence)
            return None
        if target_qty <= 0:
            logger.warning("Signal %s target_qty <= 0", key)
            return None

        # ── Universe check ──────────────────────────────────────────────────
        if symbol not in self._cfg.symbols:
            logger.debug("Signal %s symbol %s not in universe — skipping", key, symbol)
            return None

        # ── Staleness check ─────────────────────────────────────────────────
        age_s = (datetime.now(timezone.utc) - generated_at).total_seconds()
        if age_s > self._cfg.s3.max_signal_age_s:
            logger.warning("Signal %s is stale: age=%.0fs limit=%ds",
                           key, age_s, self._cfg.s3.max_signal_age_s)
            return None

        # ── Strength filter ─────────────────────────────────────────────────
        if abs(signal) < self._cfg.risk.min_signal_strength:
            logger.debug("Signal %s too weak: |%.3f| < %.3f",
                         key, signal, self._cfg.risk.min_signal_strength)
            return None

        if confidence < self._cfg.risk.min_confidence:
            logger.debug("Signal %s low confidence: %.3f < %.3f",
                         key, confidence, self._cfg.risk.min_confidence)
            return None

        return AlphaSignal(
            symbol=symbol,
            signal=signal,
            confidence=confidence,
            target_qty=target_qty,
            limit_price_bps=limit_price_bps,
            generated_at=generated_at,
        )
