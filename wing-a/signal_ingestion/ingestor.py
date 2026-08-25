# wing_a/signal_ingestion/ingestor.py
# ─────────────────────────────────────────────────────────────────────────────
#  SIGNAL INGESTOR
#  Polls the local signal directory every N seconds for signal files
#  written by Wing B's scorer / analyst publisher.
#  Validates each signal (schema, staleness, universe membership).
#  Emits validated AlphaSignal dataclasses to a callback.
#
#  Flow:  Local signals folder (Wing B writes) → poll → validate → AlphaSignal callback
#
#  Message contract with Wing B (JSON on disk):
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
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Set

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
    Runs a background thread that polls the local signal directory every poll_interval_s seconds.
    On each poll, lists new signal files, parses each one,
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

        # Track which files we've already processed so we don't re-emit on each poll
        self._seen_keys: Set[str] = set()

        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="signal-ingestor",
            daemon=True,    # daemon=True: thread dies when main thread exits
        )

    def start(self) -> None:
        logger.info("SignalIngestor starting — dir=%s poll=%ds",
                    self._cfg.signals.signal_dir,
                    self._cfg.signals.poll_interval_s)
        self._thread.start()

    def stop(self) -> None:
        logger.info("SignalIngestor stopping")
        self._stop_event.set()        # Signal the loop to exit
        self._thread.join(timeout=5)  # Wait up to 5s for clean exit

    # ── Private ───────────────────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        """Main loop: poll local signal directory, process new files, sleep, repeat."""
        while not self._stop_event.is_set():
            try:
                self._poll_once()
            except Exception as e:
                # Never crash the polling thread on a transient error
                logger.error("SignalIngestor poll error: %s", e, exc_info=True)

            # Wait for poll_interval_s, but wake immediately if stop() is called
            self._stop_event.wait(timeout=self._cfg.signals.poll_interval_s)

    def _poll_once(self) -> None:
        """Scan local signal directories, parse new files, validate, emit."""
        configured_dir = Path(self._cfg.signals.signal_dir)
        candidates = [
            configured_dir,
            Path(os.getenv("LOCAL_SIGNAL_DIR", "")) if os.getenv("LOCAL_SIGNAL_DIR") else None,
            Path(__file__).resolve().parents[2] / "wing-bb" / "signals_out" / "signals",
            Path("signals_out") / "signals",
        ]

        found_dirs = [d for d in candidates if d and d.exists()]
        if not found_dirs:
            logger.debug("No local signal directory found among: %s", candidates)
            return

        new_count = 0
        # Use first existing unique directory
        unique_dirs = []
        seen_dir_paths = set()
        for d in found_dirs:
            resolved = str(d.resolve())
            if resolved not in seen_dir_paths:
                seen_dir_paths.add(resolved)
                unique_dirs.append(d)

        for d in unique_dirs:
            for file_path in d.rglob("*.json"):
                key = str(file_path.resolve())
                if key in self._seen_keys:
                    continue

                signal = self._parse_file(file_path)
                if signal is not None:
                    self._on_signal(signal)
                    new_count += 1

                self._seen_keys.add(key)

        if new_count > 0:
            logger.info("SignalIngestor emitted %d new signals", new_count)

    def _parse_file(self, file_path: Path) -> Optional[AlphaSignal]:
        """Read one local JSON file and parse it into an AlphaSignal."""
        try:
            raw = file_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.error("Could not read file %s: %s", file_path, e)
            return None

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning("Malformed JSON in %s: %s", file_path, e)
            return None

        return self._validate(data, key=file_path.name)

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
        if age_s > self._cfg.signals.max_signal_age_s:
            logger.warning("Signal %s is stale: age=%.0fs limit=%ds",
                           key, age_s, self._cfg.signals.max_signal_age_s)
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
