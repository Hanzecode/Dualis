# wing_a/gateway/fill_subscriber.py
# ─────────────────────────────────────────────────────────────────────────────
#  FILL SUBSCRIBER
#  Subscribes to the C++ engine's ZMQ PUB socket on port 5556.
#  The C++ ExecutionEngine publishes every fill as:
#    "fills {JSON}"
#  where JSON has keys: trade_id, symbol, price_bps, price_dollars,
#                       quantity, maker_order_id, taker_order_id, timestamp_ms
#
#  This class runs a background thread, parses each message, and calls
#  on_fill(dict) for every fill — routed to both OrderManager and PnLTracker.
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import json
import logging
import threading
from typing import Callable

import zmq

from config.settings import Settings, settings as default_settings

logger = logging.getLogger(__name__)


class FillSubscriber:
    """
    Subscribes to fills published by the C++ ExecutionEngine.
    One background daemon thread runs the receive loop.
    Thread-safe: on_fill callback is called from that thread.
    """

    TOPIC = "fills"   # Must match publish_fill() prefix in execution_engine.hpp

    def __init__(
        self,
        on_fill: Callable[[dict], None],
        cfg: Settings = default_settings,
    ):
        self._on_fill = on_fill   # → OrderManager.on_fill + PnLTracker.on_fill
        self._cfg = cfg

        self._ctx = zmq.Context.instance()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._recv_loop,
            name="fill-subscriber",
            daemon=True,
        )

    def start(self) -> None:
        logger.info("FillSubscriber starting — endpoint=%s topic=%s",
                    self._cfg.zmq.sub_endpoint, self.TOPIC)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3)

    def _recv_loop(self) -> None:
        """Blocking receive loop — runs on daemon thread."""
        socket = self._ctx.socket(zmq.SUB)
        socket.setsockopt(zmq.LINGER, 0)
        socket.setsockopt(zmq.RCVTIMEO, 500)    # 500ms timeout → check stop flag
        socket.setsockopt_string(zmq.SUBSCRIBE, self.TOPIC)
        socket.connect(self._cfg.zmq.sub_endpoint)

        logger.debug("FillSubscriber SUB socket connected")

        while not self._stop.is_set():
            try:
                raw = socket.recv_string()   # Blocks up to RCVTIMEO ms
            except zmq.Again:
                continue   # Timeout — check stop flag and try again
            except zmq.ZMQError as e:
                if not self._stop.is_set():
                    logger.error("FillSubscriber ZMQ error: %s", e)
                break

            self._handle(raw)

        socket.close()
        logger.info("FillSubscriber stopped")

    def _handle(self, raw: str) -> None:
        """Parse one "fills {JSON}" message and dispatch."""
        # Strip the topic prefix: "fills {...}" → "{...}"
        if not raw.startswith(self.TOPIC):
            logger.warning("FillSubscriber unexpected message: %r", raw[:80])
            return

        payload = raw[len(self.TOPIC):].strip()

        try:
            fill = json.loads(payload)
        except json.JSONDecodeError as e:
            logger.error("FillSubscriber JSON parse error: %s — raw=%r", e, payload[:80])
            return

        # Validate expected fields
        required = {"trade_id", "symbol", "price_bps", "quantity", "timestamp_ms"}
        if not required.issubset(fill.keys()):
            logger.warning("Fill missing fields: %s", required - fill.keys())
            return

        logger.debug("Fill received: symbol=%s qty=%d price=$%.2f",
                     fill.get("symbol"), fill.get("quantity"),
                     fill.get("price_dollars", fill.get("price_bps", 0) / 10_000))

        try:
            self._on_fill(fill)
        except Exception as e:
            logger.error("on_fill callback error: %s", e, exc_info=True)
