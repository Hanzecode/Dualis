# wing_a/order_manager/manager.py
# ─────────────────────────────────────────────────────────────────────────────
#  ORDER MANAGER
#  Receives approved orders from RiskGate.
#  Splits large orders into child slices (TWAP/VWAP).
#  Tracks each order through a state machine.
#  Sends child orders to the C++ engine via ZeroMQ PUSH socket.
#  Handles fills from the ZMQ SUB socket (fill_subscriber.py feeds these in).
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Dict, List, Optional
from uuid import uuid4

import zmq   # pip install pyzmq

from config.settings import Settings, settings as default_settings
from portfolio.constructor import Order

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  ORDER STATE MACHINE
#  States mirror the C++ OrderStatus enum exactly.
# ─────────────────────────────────────────────────────────────────────────────

class OrderState(Enum):
    NEW              = auto()   # Created, not yet sent to C++ engine
    SENT             = auto()   # Sent via ZMQ PUSH, awaiting C++ ack
    OPEN             = auto()   # C++ engine confirmed it's resting in book
    PARTIALLY_FILLED = auto()   # Some quantity filled
    FILLED           = auto()   # Fully executed
    CANCELLED        = auto()   # Cancelled before full execution
    REJECTED         = auto()   # Rejected by C++ risk gate


@dataclass
class ManagedOrder:
    """Tracks a parent order and its TWAP/VWAP child slices."""
    parent_id:    str             # Our internal UUID
    symbol:       str
    side:         str
    total_qty:    int             # Total shares to execute
    filled_qty:   int   = 0
    state:        OrderState = OrderState.NEW
    child_orders: List[dict] = field(default_factory=list)   # ZMQ message dicts sent
    cpp_order_id: Optional[int] = None   # ID assigned by C++ engine (from fill)
    created_at:   float = field(default_factory=time.monotonic)
    retries:      int   = 0
    max_retries:  int   = 3

    @property
    def remaining_qty(self) -> int:
        return self.total_qty - self.filled_qty

    @property
    def is_done(self) -> bool:
        return self.state in (OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED)


# ─────────────────────────────────────────────────────────────────────────────
#  ORDER MANAGER
# ─────────────────────────────────────────────────────────────────────────────

class OrderManager:
    """
    Manages order lifecycle from risk approval to fill confirmation.

    ZMQ threading model:
    - PUSH socket created in __init__ on the main thread.
    - All sends happen on the calling thread (on_order is called from risk gate thread).
    - ZMQ PUSH socket is not thread-safe in general; we protect with _zmq_lock.
    """

    # TWAP slice interval (seconds) — send one slice every N seconds
    TWAP_INTERVAL_S: float = 30.0

    # Maximum single-order size before TWAP splitting kicks in
    # Set high enough that test orders (≤ 1000 shares) never trigger TWAP
    TWAP_THRESHOLD: int = 500

    def __init__(
        self,
        on_fill: Callable[[dict], None],   # Called when a fill is confirmed
        cfg: Settings = default_settings,
    ):
        self._on_fill = on_fill   # → PnLTracker
        self._cfg = cfg

        # Active orders — keyed by our internal parent_id
        self._orders: Dict[str, ManagedOrder] = {}
        self._lock = threading.Lock()

        # ZMQ PUSH socket → C++ engine PULL (push_pull_receiver.hpp)
        self._zmq_ctx = zmq.Context.instance()
        self._push_socket = self._zmq_ctx.socket(zmq.PUSH)
        self._push_socket.setsockopt(zmq.LINGER, 0)     # Don't block on close
        self._push_socket.setsockopt(zmq.SNDHWM, 1000)  # High-water mark: 1000 msgs
        self._push_socket.connect(cfg.zmq.push_endpoint)
        self._zmq_lock = threading.Lock()

        logger.info("OrderManager connected PUSH → %s", cfg.zmq.push_endpoint)

    def on_order(self, order: Order) -> None:
        """
        Called by RiskGate with an approved order.
        Decides whether to send immediately or slice via TWAP.
        """
        parent_id = str(uuid4())
        managed = ManagedOrder(
            parent_id=parent_id,
            symbol=order.symbol,
            side=order.side,
            total_qty=order.quantity,
        )

        with self._lock:
            self._orders[parent_id] = managed

        if order.quantity > self.TWAP_THRESHOLD:
            self._execute_twap(managed, order)
        else:
            self._send_child(managed, order.quantity, order.price_bps, order.order_type)

    def on_fill(self, fill: dict) -> None:
        """
        Called by FillSubscriber when C++ engine publishes a fill.
        fill dict keys: trade_id, symbol, price_bps, quantity,
                        maker_order_id, taker_order_id, timestamp_ms
        """
        symbol = fill.get("symbol", "")
        qty    = fill.get("quantity", 0)
        price  = fill.get("price_bps", 0)

        # Find the managed order for this symbol
        with self._lock:
            for managed in self._orders.values():
                if managed.symbol == symbol and not managed.is_done:
                    managed.filled_qty += qty
                    if managed.filled_qty >= managed.total_qty:
                        managed.state = OrderState.FILLED
                        logger.info("[%s] Order %s FILLED — total qty=%d",
                                    symbol, managed.parent_id, managed.total_qty)
                    else:
                        managed.state = OrderState.PARTIALLY_FILLED
                    break

        # NOTE: previously called self._on_fill(fill) here to "forward to
        # PnL tracker" — but main.py wires OrderManager's on_fill callback
        # to Wing A's fan-out method (_on_fill_from_engine), which is the
        # SAME method that calls OrderManager.on_fill() in the first place
        # (see main.py's own architecture diagram: FillSubscriber fans out
        # to OrderManager and PnLTracker as parallel siblings, not chained).
        # Calling self._on_fill(fill) here re-entered _on_fill_from_engine,
        # which called order_manager.on_fill(fill) again — infinite mutual
        # recursion on every single fill, silently swallowed by
        # FillSubscriber's try/except, so fills never reached PnLTracker or
        # the dashboard. _on_fill_from_engine already calls
        # self.pnl_tracker.on_fill(fill) directly right after this method
        # returns, so no forwarding is needed here.

    def cancel_order(self, symbol: str, cpp_order_id: int) -> None:
        """Send a CANCEL message to the C++ engine."""
        msg = json.dumps({
            "type":     "CANCEL",
            "symbol":   symbol,
            "order_id": cpp_order_id,
        })
        self._zmq_send(msg)
        logger.info("[%s] Sent CANCEL for cpp_order_id=%d", symbol, cpp_order_id)

    def shutdown(self) -> None:
        self._push_socket.close()
        logger.info("OrderManager shut down")

    @property
    def open_order_count(self) -> int:
        with self._lock:
            return sum(1 for o in self._orders.values() if not o.is_done)

    # ── Private ───────────────────────────────────────────────────────────────

    def _execute_twap(self, managed: ManagedOrder, order: Order) -> None:
        """
        TWAP (Time-Weighted Average Price) execution.
        Splits total_qty into equal slices sent every TWAP_INTERVAL_S seconds.
        Runs in a background daemon thread so it doesn't block on_order().
        """
        def twap_loop():
            # Number of slices: ceil(total / threshold) — each slice ≤ threshold
            n_slices = max(1, managed.total_qty // self.TWAP_THRESHOLD)
            slice_qty = managed.total_qty // n_slices
            remainder = managed.total_qty % n_slices

            logger.info("[%s] TWAP: total=%d slices=%d interval=%.0fs",
                        managed.symbol, managed.total_qty, n_slices, self.TWAP_INTERVAL_S)

            for i in range(n_slices):
                if managed.is_done:
                    break   # Parent was cancelled externally

                qty = slice_qty + (remainder if i == n_slices - 1 else 0)
                self._send_child(managed, qty, order.price_bps, order.order_type)
                time.sleep(self.TWAP_INTERVAL_S)

        t = threading.Thread(target=twap_loop, name=f"twap-{managed.parent_id[:8]}",
                             daemon=True)
        t.start()

    def _send_child(self, managed: ManagedOrder, qty: int,
                    price_bps: int, order_type: str) -> None:
        """Build and send one child order message via ZMQ PUSH."""
        if qty <= 0:
            return

        msg_dict = {
            "type":       "ORDER",
            "symbol":     managed.symbol,
            "side":       managed.side,
            "order_type": order_type,
            "price_bps":  price_bps,
            "quantity":   qty,
        }
        msg = json.dumps(msg_dict)

        retries = 0
        while retries <= managed.max_retries:
            if self._zmq_send(msg):
                managed.child_orders.append(msg_dict)
                managed.state = OrderState.SENT
                logger.info(
                    "[%s] PUSH sent: side=%s qty=%d price=$%.2f (slice %d/%d)",
                    managed.symbol, managed.side, qty, price_bps / 10_000,
                    len(managed.child_orders), managed.max_retries + 1
                )
                return
            retries += 1
            time.sleep(0.1 * retries)   # Exponential back-off: 0.1s, 0.2s, 0.3s

        managed.state = OrderState.REJECTED
        logger.error("[%s] Failed to send after %d retries — order REJECTED",
                     managed.symbol, managed.max_retries)

    def _zmq_send(self, msg: str) -> bool:
        """Send a raw string via the PUSH socket. Returns True on success."""
        try:
            with self._zmq_lock:
                # NOBLOCK: don't block if C++ engine is down — fail fast
                self._push_socket.send_string(msg, flags=zmq.NOBLOCK)
            return True
        except zmq.Again:
            # High-water mark reached or C++ engine not connected
            logger.warning("ZMQ PUSH would block (engine busy or not connected)")
            return False
        except zmq.ZMQError as e:
            logger.error("ZMQ PUSH error: %s", e)
            return False
